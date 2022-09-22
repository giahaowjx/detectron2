# Copyright (c) Facebook, Inc. and its affiliates.

from typing import Any, List
import torch
from torch.nn import functional as F
import numpy as np
import math

from detectron2.config import CfgNode
from detectron2.structures import Instances

from .mask_or_segm import MaskOrSegmentationLoss
from .registry import DENSEPOSE_LOSS_REGISTRY
from densepose.modeling.correction import CorrectorPredictorOutput
from .utils import (
    BilinearInterpolationHelper,
    ChartBasedAnnotationsAccumulator,
    LossDict,
    extract_packed_annotations_from_matches,
    resample_data,
)


@DENSEPOSE_LOSS_REGISTRY.register()
class DensePoseChartLoss:
    """
    DensePose loss for chart-based training. A mesh is split into charts,
    each chart is given a label (I) and parametrized by 2 coordinates referred to
    as U and V. Ground truth consists of a number of points annotated with
    I, U and V values and coarse segmentation S defined for all pixels of the
    object bounding box. In some cases (see `COARSE_SEGM_TRAINED_BY_MASKS`),
    semantic segmentation annotations can be used as ground truth inputs as well.

    Estimated values are tensors:
     * U coordinates, tensor of shape [N, C, S, S]
     * V coordinates, tensor of shape [N, C, S, S]
     * fine segmentation estimates, tensor of shape [N, C, S, S] with raw unnormalized
       scores for each fine segmentation label at each location
     * coarse segmentation estimates, tensor of shape [N, D, S, S] with raw unnormalized
       scores for each coarse segmentation label at each location
    where N is the number of detections, C is the number of fine segmentation
    labels, S is the estimate size ( = width = height) and D is the number of
    coarse segmentation channels.

    The losses are:
    * regression (smooth L1) loss for U and V coordinates
    * cross entropy loss for fine (I) and coarse (S) segmentations
    Each loss has an associated weight
    """

    def __init__(self, cfg: CfgNode):
        """
        Initialize chart-based loss from configuration options

        Args:
            cfg (CfgNode): configuration options
        """
        # fmt: off
        self.heatmap_size = cfg.MODEL.ROI_DENSEPOSE_HEAD.HEATMAP_SIZE
        self.w_points     = cfg.MODEL.ROI_DENSEPOSE_HEAD.POINT_REGRESSION_WEIGHTS
        self.w_part       = cfg.MODEL.ROI_DENSEPOSE_HEAD.PART_WEIGHTS
        self.w_segm       = cfg.MODEL.ROI_DENSEPOSE_HEAD.INDEX_WEIGHTS
        self.n_segm_chan  = cfg.MODEL.ROI_DENSEPOSE_HEAD.NUM_COARSE_SEGM_CHANNELS
        # fmt: on
        self.segm_trained_by_masks = cfg.MODEL.ROI_DENSEPOSE_HEAD.COARSE_SEGM_TRAINED_BY_MASKS
        self.segm_loss = MaskOrSegmentationLoss(cfg)

        # self.w_pseudo     = cfg.MODEL.SEMI.UNSUP_WEIGHTS
        self.w_p_segm     = cfg.MODEL.SEMI.SEGM_WEIGHTS
        self.w_p_points   = cfg.MODEL.SEMI.POINTS_WEIGHTS
        self.pseudo_threshold = cfg.MODEL.SEMI.THRESHOLD
        self.n_channels = cfg.MODEL.ROI_DENSEPOSE_HEAD.NUM_PATCHES + 1
        self.loss_name = cfg.MODEL.SEMI.LOSS_NAME
        self.uv_loss_channels = cfg.MODEL.SEMI.UV_LOSS_CHANNELS

        self.w_crt_points = cfg.MODEL.SEMI.COR.POINTS_WEIGHTS
        self.w_crt_segm = cfg.MODEL.SEMI.COR.SEGM_WEIGHTS

        self.log2pi = math.log(2 * math.pi)

    def __call__(
        self, proposals_with_gt: List[Instances], densepose_predictor_outputs: Any, corrections: CorrectorPredictorOutput =None, **kwargs
    ) -> LossDict:
        """
        Produce chart-based DensePose losses

        Args:
            proposals_with_gt (list of Instances): detections with associated ground truth data
            densepose_predictor_outputs: an object of a dataclass that contains predictor outputs
                with estimated values; assumed to have the following attributes:
                * coarse_segm - coarse segmentation estimates, tensor of shape [N, D, S, S]
                * fine_segm - fine segmentation estimates, tensor of shape [N, C, S, S]
                * u - U coordinate estimates per fine labels, tensor of shape [N, C, S, S]
                * v - V coordinate estimates per fine labels, tensor of shape [N, C, S, S]
            where N is the number of detections, C is the number of fine segmentation
            labels, S is the estimate size ( = width = height) and D is the number of
            coarse segmentation channels.

        Return:
            dict: str -> tensor: dict of losses with the following entries:
             * `loss_densepose_U`: smooth L1 loss for U coordinate estimates
             * `loss_densepose_V`: smooth L1 loss for V coordinate estimates
             * `loss_densepose_I`: cross entropy for raw unnormalized scores for fine
                 segmentation estimates given ground truth labels;
             * `loss_densepose_S`: cross entropy for raw unnormalized scores for coarse
                 segmentation estimates given ground truth labels;
        """
        # densepose outputs are computed for all images and all bounding boxes;
        # i.e. if a batch has 4 images with (3, 1, 2, 1) proposals respectively,
        # the outputs will have size(0) == 3+1+2+1 == 7

        if not len(proposals_with_gt):
            return self.produce_fake_densepose_losses(densepose_predictor_outputs, corrections=corrections)

        accumulator = ChartBasedAnnotationsAccumulator()
        packed_annotations = extract_packed_annotations_from_matches(proposals_with_gt, accumulator)

        # NOTE: we need to keep the same computation graph on all the GPUs to
        # perform reduction properly. Hence even if we have no data on one
        # of the GPUs, we still need to generate the computation graph.
        # Add fake (zero) loss in the form Tensor.sum() * 0
        if packed_annotations is None:
            return self.produce_fake_densepose_losses(densepose_predictor_outputs, corrections=corrections)

        h, w = densepose_predictor_outputs.u.shape[2:]
        interpolator = BilinearInterpolationHelper.from_matches(
            packed_annotations,
            (h, w),
        )

        j_valid_fg = interpolator.j_valid * (  # pyre-ignore[16]
            packed_annotations.fine_segm_labels_gt > 0
        )
        if not torch.any(j_valid_fg):
            return self.produce_fake_densepose_losses(densepose_predictor_outputs, corrections=corrections)

        losses_uv = self.produce_densepose_losses_uv(
            proposals_with_gt,
            densepose_predictor_outputs,
            packed_annotations,
            interpolator,
            j_valid_fg,  # pyre-ignore[6]
            corrections=corrections,
        )

        losses_segm = self.produce_densepose_losses_segm(
            proposals_with_gt,
            densepose_predictor_outputs,
            packed_annotations,
            interpolator,
            j_valid_fg,  # pyre-ignore[6]
            corrections=corrections,
        )
        
        # losses_segm = {"loss_densepose_S": self.segm_loss(proposals_with_gt, densepose_predictor_outputs,
        #                                                  packed_annotations) * self.w_segm}

        losses_unsup = self.produce_densepose_losses_unsup(
            proposals_with_gt,
            densepose_predictor_outputs,
            packed_annotations,
        )

        return {**losses_uv, **losses_segm, **losses_unsup}
        # return {**losses_segm, **losses_unsup}

    def produce_fake_densepose_losses(self, densepose_predictor_outputs: Any, corrections: CorrectorPredictorOutput = None) -> LossDict:
        """
        Fake losses for fine segmentation and U/V coordinates. These are used when
        no suitable ground truth data was found in a batch. The loss has a value 0
        and is primarily used to construct the computation graph, so that
        `DistributedDataParallel` has similar graphs on all GPUs and can perform
        reduction properly.

        Args:
            densepose_predictor_outputs: DensePose predictor outputs, an object
                of a dataclass that is assumed to have the following attributes:
             * fine_segm - fine segmentation estimates, tensor of shape [N, C, S, S]
             * u - U coordinate estimates per fine labels, tensor of shape [N, C, S, S]
             * v - V coordinate estimates per fine labels, tensor of shape [N, C, S, S]
        Return:
            dict: str -> tensor: dict of losses with the following entries:
             * `loss_densepose_U`: has value 0
             * `loss_densepose_V`: has value 0
             * `loss_densepose_I`: has value 0
             * `loss_densepose_S`: has value 0
        """
        losses_uv = self.produce_fake_densepose_losses_uv(densepose_predictor_outputs, corrections=corrections)
        losses_segm = self.produce_fake_densepose_losses_segm(densepose_predictor_outputs, corrections=corrections)
        # losses_segm = {"loss_densepose_S": self.segm_loss.fake_value(densepose_predictor_outputs)}
        losses_unsup = self.produce_fake_densepose_losses_unsup(densepose_predictor_outputs)
        return {**losses_uv, **losses_segm, **losses_unsup}
        # return {**losses_segm, **losses_unsup}

    def produce_fake_densepose_losses_uv(self, densepose_predictor_outputs: Any, corrections: CorrectorPredictorOutput =None) -> LossDict:
        """
        Fake losses for U/V coordinates. These are used when no suitable ground
        truth data was found in a batch. The loss has a value 0
        and is primarily used to construct the computation graph, so that
        `DistributedDataParallel` has similar graphs on all GPUs and can perform
        reduction properly.

        Args:
            densepose_predictor_outputs: DensePose predictor outputs, an object
                of a dataclass that is assumed to have the following attributes:
             * u - U coordinate estimates per fine labels, tensor of shape [N, C, S, S]
             * v - V coordinate estimates per fine labels, tensor of shape [N, C, S, S]
        Return:
            dict: str -> tensor: dict of losses with the following entries:
             * `loss_densepose_U`: has value 0
             * `loss_densepose_V`: has value 0
        """
        losses = {
            "loss_densepose_U": densepose_predictor_outputs.u.sum() * 0,
            "loss_densepose_V": densepose_predictor_outputs.v.sum() * 0,
        }

        if corrections is not None:
            losses.update({
                "loss_correction_U": corrections.u.sum() * 0,
                "loss_correction_V": corrections.v.sum() * 0,
                # "loss_correction_UV": corrections.u.sum() * 0
            })

        return losses

    def produce_fake_densepose_losses_unsup(self, densepose_predictor_outputs: Any) -> LossDict:
        return {
            "loss_unsup_segm": densepose_predictor_outputs.fine_segm.sum() * 0,
            "loss_u_p": densepose_predictor_outputs.u.sum() * 0,
            "loss_v_p": densepose_predictor_outputs.v.sum() * 0,
        }

    def produce_fake_densepose_losses_segm(self, densepose_predictor_outputs: Any, corrections: CorrectorPredictorOutput = None) -> LossDict:
        """
        Fake losses for fine / coarse segmentation. These are used when
        no suitable ground truth data was found in a batch. The loss has a value 0
        and is primarily used to construct the computation graph, so that
        `DistributedDataParallel` has similar graphs on all GPUs and can perform
        reduction properly.

        Args:
            densepose_predictor_outputs: DensePose predictor outputs, an object
                of a dataclass that is assumed to have the following attributes:
             * fine_segm - fine segmentation estimates, tensor of shape [N, C, S, S]
             * coarse_segm - coarse segmentation estimates, tensor of shape [N, D, S, S]
        Return:
            dict: str -> tensor: dict of losses with the following entries:
             * `loss_densepose_I`: has value 0
             * `loss_densepose_S`: has value 0, added only if `segm_trained_by_masks` is False
        """
        losses = {
            "loss_densepose_I": densepose_predictor_outputs.fine_segm.sum() * 0,
            "loss_densepose_S": self.segm_loss.fake_value(densepose_predictor_outputs),
        }

        if corrections is not None:
            losses.update({
                "loss_correction_I": corrections.fine_segm.sum() * 0,
            })

        return losses

    def produce_densepose_losses_uv(
        self,
        proposals_with_gt: List[Instances],
        densepose_predictor_outputs: Any,
        packed_annotations: Any,
        interpolator: BilinearInterpolationHelper,
        j_valid_fg: torch.Tensor,
        corrections: CorrectorPredictorOutput=None,
    ) -> LossDict:
        """
        Compute losses for U/V coordinates: smooth L1 loss between
        estimated coordinates and the ground truth.

        Args:
            proposals_with_gt (list of Instances): detections with associated ground truth data
            densepose_predictor_outputs: DensePose predictor outputs, an object
                of a dataclass that is assumed to have the following attributes:
             * u - U coordinate estimates per fine labels, tensor of shape [N, C, S, S]
             * v - V coordinate estimates per fine labels, tensor of shape [N, C, S, S]
        Return:
            dict: str -> tensor: dict of losses with the following entries:
             * `loss_densepose_U`: smooth L1 loss for U coordinate estimates
             * `loss_densepose_V`: smooth L1 loss for V coordinate estimates
        """
        u_gt = packed_annotations.u_gt[j_valid_fg]
        u_est = interpolator.extract_at_points(densepose_predictor_outputs.u)[j_valid_fg]
        v_gt = packed_annotations.v_gt[j_valid_fg]
        v_est = interpolator.extract_at_points(densepose_predictor_outputs.v)[j_valid_fg]
        loss = {
            "loss_densepose_U": F.smooth_l1_loss(u_est, u_gt, reduction="sum") * self.w_points,
            "loss_densepose_V": F.smooth_l1_loss(v_est, v_gt, reduction="sum") * self.w_points,
        }

        if corrections is not None:
            u_crt_est = interpolator.extract_at_points(corrections.u)[j_valid_fg]
            v_crt_est = interpolator.extract_at_points(corrections.v)[j_valid_fg]

            # loss distribution
            # sigma2 = F.softplus(u_crt_est) + 0.01
        
            with torch.no_grad():
                u_crt_gt = u_gt - u_est.clamp(0., 1.)
                v_crt_gt = v_gt - v_est.clamp(0., 1.)
                # delta_t_delta = (u_gt - u_est.clamp(0., 1.)) ** 2 + (v_gt - v_est.clamp(0., 1.)) ** 2

            # uv_loss = 0.5 * (self.log2pi + 2 * torch.log(sigma2) + delta_t_delta / sigma2)
            loss.update({
                "loss_correction_U": F.smooth_l1_loss(u_crt_est, u_crt_gt, reduction='sum') * self.w_crt_points,
                "loss_correction_V": F.smooth_l1_loss(v_crt_est, v_crt_gt, reduction='sum') * self.w_crt_points,
                # "loss_correction_UV": uv_loss.sum() * self.w_crt_points
            })

        return loss

    def produce_densepose_losses_segm(
        self,
        proposals_with_gt: List[Instances],
        densepose_predictor_outputs: Any,
        packed_annotations: Any,
        interpolator: BilinearInterpolationHelper,
        j_valid_fg: torch.Tensor,
        corrections: CorrectorPredictorOutput=None,
    ) -> LossDict:
        """
        Losses for fine / coarse segmentation: cross-entropy
        for segmentation unnormalized scores given ground truth labels at
        annotated points for fine segmentation and dense mask annotations
        for coarse segmentation.

        Args:
            proposals_with_gt (list of Instances): detections with associated ground truth data
            densepose_predictor_outputs: DensePose predictor outputs, an object
                of a dataclass that is assumed to have the following attributes:
             * fine_segm - fine segmentation estimates, tensor of shape [N, C, S, S]
             * coarse_segm - coarse segmentation estimates, tensor of shape [N, D, S, S]
        Return:
            dict: str -> tensor: dict of losses with the following entries:
             * `loss_densepose_I`: cross entropy for raw unnormalized scores for fine
                 segmentation estimates given ground truth labels
             * `loss_densepose_S`: cross entropy for raw unnormalized scores for coarse
                 segmentation estimates given ground truth labels;
                 may be included if coarse segmentation is only trained
                 using DensePose ground truth; if additional supervision through
                 instance segmentation data is performed (`segm_trained_by_masks` is True),
                 this loss is handled by `produce_mask_losses` instead
        """
        fine_segm_gt = packed_annotations.fine_segm_labels_gt[
            interpolator.j_valid  # pyre-ignore[16]
        ]
        fine_segm_est = interpolator.extract_at_points(
            densepose_predictor_outputs.fine_segm,
            slice_fine_segm=slice(None),
            w_ylo_xlo=interpolator.w_ylo_xlo[:, None],  # pyre-ignore[16]
            w_ylo_xhi=interpolator.w_ylo_xhi[:, None],  # pyre-ignore[16]
            w_yhi_xlo=interpolator.w_yhi_xlo[:, None],  # pyre-ignore[16]
            w_yhi_xhi=interpolator.w_yhi_xhi[:, None],  # pyre-ignore[16]
        )[interpolator.j_valid, :]
        loss = {
            "loss_densepose_I": F.cross_entropy(fine_segm_est, fine_segm_gt.long()) * self.w_part,
            "loss_densepose_S": self.segm_loss(
                proposals_with_gt, densepose_predictor_outputs, packed_annotations
            )
            * self.w_segm,
        }

        if corrections is not None:
            fine_segm_crt_est = interpolator.extract_at_points(
                corrections.fine_segm,
                slice_fine_segm=slice(None),
                w_ylo_xlo=interpolator.w_ylo_xlo[:, None],  # pyre-ignore[16]
                w_ylo_xhi=interpolator.w_ylo_xhi[:, None],  # pyre-ignore[16]
                w_yhi_xlo=interpolator.w_yhi_xlo[:, None],  # pyre-ignore[16]
                w_yhi_xhi=interpolator.w_yhi_xhi[:, None],  # pyre-ignore[16]
            )[interpolator.j_valid, :]
            
            with torch.no_grad():
                fine_segm_crt_gt = torch.ones_like(fine_segm_gt) * self.n_channels
                index = fine_segm_est.argmax(dim=1).long() != fine_segm_gt
                fine_segm_crt_gt[index] = fine_segm_gt[index]

            crt_loss = F.cross_entropy(fine_segm_crt_est, fine_segm_crt_gt.long(), reduction='none')
            crt_loss[~index] = crt_loss[~index] * 2
            loss.update({
                "loss_correction_I": crt_loss.mean() * self.w_crt_segm
            })

        return loss


    def produce_densepose_losses_unsup(
        self,
        proposals_with_gt: List[Instances],
        densepose_predictor_outputs: Any,
        packed_annotations: Any,
    ) -> LossDict:
        """
        Losses for pseudo segm and U V coordinate: cross-entropy/smooth l1 loss
        for segmentation unnormalized scores given ground truth labels at
        annotated points for fine segmentation and dense mask annotations
        for coarse segmentation.

        Args:
            proposals_with_gt (list of Instances): detections with associated ground truth data
            densepose_predictor_outputs: DensePose predictor outputs, an object
                of a dataclass that is assumed to have the following attributes:
             * fine_segm - fine segmentation estimates, tensor of shape [N, C, S, S]
             * u - U coordinate estimates per fine labels, tensor of shape [N, C, S, S]
             * v - V coordinate estimates per fine labels, tensor of shape [N, C, S, S]
        Return:
            dict: str -> tensor: dict of losses with the following entries:
             * `loss_densepose_PS`: cross entropy for raw unnormalized scores for fine
                 segmentation estimates given pseudo truth labels
             * `loss_densepose_PU`: smooth L1 loss for U coordinate estimates
             * `loss_densepose_PV`: smooth L1 loss for V coordinate estimates
        """

        losses = {}
        pseudo_keys = ["fine_segm_p", "u_p", "v_p"]
        est_keys = ["fine_segm", "u", "v"]
        mask = None
        pred_index = None
        weights = None

        with torch.no_grad():
            pos_index = resample_data(
                packed_annotations.coarse_segm_gt.unsqueeze(1),
                packed_annotations.bbox_xywh_gt,
                packed_annotations.bbox_xywh_est,
                self.heatmap_size,
                self.heatmap_size,
                mode="nearest",
                padding_mode="zeros",
            ).squeeze(1) > 0
            pos_index = pos_index.reshape(-1)

        for p_key, e_key in zip(pseudo_keys, est_keys):
            if getattr(packed_annotations, p_key) is None:
                return self.produce_fake_densepose_losses_unsup(densepose_predictor_outputs)
            est = getattr(densepose_predictor_outputs, e_key)[packed_annotations.bbox_indices]
            est = est.permute(0, 2, 3, 1).reshape(-1, self.n_channels)[pos_index]
            with torch.no_grad():
                pseudo = getattr(packed_annotations, p_key)
                pseudo = resample_data(
                    pseudo,
                    packed_annotations.bbox_xywh_gt,
                    packed_annotations.bbox_xywh_est,
                    self.heatmap_size,
                    self.heatmap_size,
                    mode="nearest",
                    padding_mode="zeros",
                )
            pseudo = pseudo.permute(0, 2, 3, 1).reshape(-1, self.n_channels)[pos_index]
            if p_key == "fine_segm_p":
                # prediction = F.softmax(pseudo, dim=1)
                segm_conf = torch.max(pseudo, dim=1).values
                pred_index = torch.argsort(pseudo, dim=1, descending=True)[:, :self.uv_loss_channels]
                if self.pseudo_threshold < 1:
                    # if filter pseudo labels using segm confidence
                    mask = segm_conf >= self.pseudo_threshold
                    if mask.sum() <= 0:
                        return self.produce_fake_densepose_losses_unsup(densepose_predictor_outputs)
                    pred_index = pred_index[mask].long()
                    est = est[mask]
                    segm_conf = segm_conf[mask]
                # pred_index[~pos_index] = 0
                # weights = segm_conf / segm_conf.sum()
                loss = F.cross_entropy(est, pred_index[:, 0].long(), reduction='none')
                if self.loss_name == "sce":
                    label_one_hot = F.one_hot(torch.clamp(pred_index[:, 0], min=0, max=self.n_channels - 1), self.n_channels).float()
                    label_one_hot = torch.clamp(label_one_hot, min=1e-4, max=1.0)
                    rce_loss = -1 * est * torch.log(label_one_hot)
                    rce_loss = torch.sum(rce_loss, dim=1)
                    rce_loss = torch.mean(rce_loss * mask)
                    loss = loss * (1 - mask) + rce_loss
                    weights = mask / mask.sum()
                    losses.update({"loss_unsup_segm": (loss * weights).sum() * self.w_p_segm})
                elif self.loss_name == "ce":
                    # weights = segm_conf / segm_conf.sum()
                    losses.update({"loss_unsup_segm": loss.mean() * self.w_p_segm})
                    # weights = torch.cat([weights for _ in range(self.uv_loss_channels)])
                # losses.update({"loss_unsup_segm": F.cross_entropy(est, torch.argmax(pseudo, dim=1).long()) * self.w_pseudo * self.w_p_segm})
            else:
                if self.pseudo_threshold < 1:
                    pseudo = pseudo[mask]
                    est = est[mask]
                pseudo = torch.cat([pseudo[np.arange(pseudo.shape[0]), pred_index[:, i]]
                                       for i in range(self.uv_loss_channels)])
                est = torch.cat([est[np.arange(pred_index.shape[0]), pred_index[:, i]]
                                    for i in range(self.uv_loss_channels)])
                # use all uv coordinates
                # weights = weights.unsqueeze(1) / 25
                loss = F.smooth_l1_loss(est, pseudo, reduction='none')
                if self.loss_name == "sce":
                    losses.update({"loss_{}".format(p_key): (loss * weights).sum() * self.w_p_points})
                elif self.loss_name == "ce":
                    losses.update({"loss_{}".format(p_key): loss.mean() * self.w_p_points})
                # losses.update({"loss_{}".format(p_key): F.smooth_l1_loss(est, pseudo) * self.w_pseudo * self.w_p_points})
        return losses

    def fake_value(self, densepose_predictor_outputs: Any) -> LossDict:
        """
        Fake segmentation loss used when no suitable ground truth data
        was found in a batch. The loss has a value 0 and is primarily used to
        construct the computation graph, so that `DistributedDataParallel`
        has similar graphs on all GPUs and can perform reduction properly.

        Args:
            densepose_predictor_outputs: DensePose predictor outputs, an object
                of a dataclass that is assumed to have `coarse_segm`
                attribute
        Return:
            Zero value loss with proper computation graph
        """
        return {
            "loss_unsup_segm": densepose_predictor_outputs.fine_segm_p.sum() * 0,
            "loss_u_p": densepose_predictor_outputs.u_p.sum() * 0,
            "loss_v_p": densepose_predictor_outputs.v_p.sum() * 0,
        }
