# Copyright (c) Meta Platforms, Inc. and affiliates.
# Adapted from BasicVSR++ network structure: "BasicVSR++: Improving Video Super-Resolution with Enhanced Propagation and Alignment"

import torch
import torch.nn as nn
import torch.nn.functional as F
from mmcv.cnn import constant_init
from mmcv.runner import load_checkpoint

from .common import PixelShufflePack, flow_warp, ResidualBlocksWithInputConv, SPyNet, SecondOrderDeformableAlignment
from .registry import BACKBONES
from mmseg.utils import get_root_logger


@BACKBONES.register_module()
class DVSR(nn.Module):
    """
    Args:
        mid_channels (int, optional): Channel number of the intermediate
            features. Default: 64.
        num_blocks (int, optional): The number of residual blocks in each
            propagation branch. Default: 7.
        scale: dToF sensor downsampling scale. Needs to be consistent with
            loaded data.
        max_residue_magnitude (int): The maximum magnitude of the offset
            residue (Eq. 6 in paper). Default: 10.
        is_low_res_input (bool, optional): Whether the input is low-resolution
            or not. If False, the output resolution is equal to the input
            resolution. Default: True.
        spynet_pretrained (str, optional): Pre-trained model path of SPyNet.
            Default: None.
        cpu_cache_length (int, optional): When the length of sequence is larger
            than this value, the intermediate features are sent to CPU. This
            saves GPU memory, but slows down the inference speed. You can
            increase this number if you have a GPU with large memory.
            Default: 100.
    """

    def __init__(
        self,
        mid_channels=64,
        num_blocks=7,
        scale=16,
        max_residue_magnitude=10,
        is_low_res_input=True,
        spynet_pretrained=None,
        cpu_cache_length=200,
    ):
        
        super().__init__()
        self.mid_channels = mid_channels
        self.is_low_res_input = is_low_res_input
        self.scale = scale
        self.cpu_cache_length = cpu_cache_length

        # optical flow
        self.spynet = nn.ModuleDict()
        self.spynet["hg_1"] = SPyNet(pretrained=spynet_pretrained)
        self.spynet["hg_2"] = SPyNet(pretrained=spynet_pretrained)

        # feature extraction module
        self.conv_guide_init = nn.ModuleDict()
        self.conv_guide_init["hg_1"] = nn.Sequential(
            nn.Conv2d(3, mid_channels, kernel_size=3, stride=1, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            nn.Conv2d(mid_channels, mid_channels, kernel_size=3, stride=2, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            ResidualBlocksWithInputConv(mid_channels, mid_channels, 1),
            nn.Conv2d(mid_channels, mid_channels, kernel_size=3, stride=2, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            ResidualBlocksWithInputConv(mid_channels, mid_channels, 2),
        )
        self.conv_guide_init["hg_2"] = nn.Sequential(
            nn.Conv2d(2, mid_channels, kernel_size=3, stride=1, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            nn.Conv2d(mid_channels, mid_channels, kernel_size=3, stride=2, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            ResidualBlocksWithInputConv(mid_channels, mid_channels, 1),
            nn.Conv2d(mid_channels, mid_channels, kernel_size=3, stride=2, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            ResidualBlocksWithInputConv(mid_channels, mid_channels, 2),
        )
        self.feat_extract = nn.ModuleDict()
        self.feat_extract["hg_1"] = ResidualBlocksWithInputConv(
            1 + mid_channels, mid_channels, 5
        )
        self.feat_extract["hg_2"] = ResidualBlocksWithInputConv(
            1 + mid_channels * 2, mid_channels, 5
        )

        # propagation branches
        self.deform_align = nn.ModuleDict()
        self.deform_align["hg_1"] = nn.ModuleDict()
        self.deform_align["hg_2"] = nn.ModuleDict()
        self.backbone = nn.ModuleDict()
        self.backbone["hg_1"] = nn.ModuleDict()
        self.backbone["hg_2"] = nn.ModuleDict()

        modules = ["backward_1", "forward_1", "backward_2", "forward_2"]
        for i, module in enumerate(modules):
            self.deform_align["hg_1"][module] = SecondOrderDeformableAlignment(
                3,
                2 * mid_channels,
                mid_channels,
                3,
                padding=1,
                deform_groups=16,
                max_residue_magnitude=max_residue_magnitude,
            )
            self.deform_align["hg_2"][module] = SecondOrderDeformableAlignment(
                3,
                2 * mid_channels,
                mid_channels,
                3,
                padding=1,
                deform_groups=16,
                max_residue_magnitude=max_residue_magnitude,
            )
            self.backbone["hg_1"][module] = ResidualBlocksWithInputConv(
                (2 + i) * mid_channels, mid_channels, num_blocks
            )
            self.backbone["hg_2"][module] = ResidualBlocksWithInputConv(
                (2 + i) * mid_channels, mid_channels, num_blocks
            )

        # activation function
        self.lrelu = nn.LeakyReLU(negative_slope=0.1, inplace=True)

        # upsampling module
        self.reconstruction = nn.ModuleDict()
        self.reconstruction["hg_1"] = ResidualBlocksWithInputConv(
            5 * mid_channels, mid_channels, 5
        )
        self.reconstruction["hg_2"] = ResidualBlocksWithInputConv(
            5 * mid_channels, mid_channels, 5
        )

        self.final_pred = nn.ModuleDict()
        self.final_pred["hg_1"] = nn.Sequential(
            PixelShufflePack(mid_channels, mid_channels, 2, upsample_kernel=3),
            self.lrelu,
            PixelShufflePack(mid_channels, 64, 2, upsample_kernel=3),
            self.lrelu,
            nn.Conv2d(64, 64, 3, 1, 1),
            self.lrelu,
            nn.Conv2d(64, 2, 3, 1, 1),
        )

        self.final_pred["hg_2"] = nn.Sequential(
            PixelShufflePack(mid_channels, mid_channels, 2, upsample_kernel=3),
            self.lrelu,
            PixelShufflePack(mid_channels, 64, 2, upsample_kernel=3),
            self.lrelu,
            nn.Conv2d(64, 64, 3, 1, 1),
            self.lrelu,
            nn.Conv2d(64, 2, 3, 1, 1),
        )

        self.img_upsample = nn.Upsample(
            scale_factor=4, mode="bilinear", align_corners=False
        )
        self.softmax = nn.Softmax(dim=2)

        # check if the sequence is augmented by flipping
        self.is_mirror_extended = False

    def check_if_mirror_extended(self, lqs):
        """Check whether the input is a mirror-extended sequence.
        If mirror-extended, the i-th (i=0, ..., t-1) frame is equal to the
        (t-1-i)-th frame.
        Args:
            lqs (tensor): Input low quality (LQ) sequence with
                shape (n, t, c, h, w).
        """

        if lqs.size(1) % 2 == 0:
            lqs_1, lqs_2 = torch.chunk(lqs, 2, dim=1)
            if torch.norm(lqs_1 - lqs_2.flip(1)) == 0:
                self.is_mirror_extended = True

    def compute_flow(self, guides, hg_idx):
        """Compute optical flow using SPyNet for feature alignment.
        Note that if the input is an mirror-extended sequence, 'flows_forward'
        is not needed, since it is equal to 'flows_backward.flip(1)'.
        Args:
            guides (tensor): Input low quality (LQ) sequence with
                shape (n, t, c, h, w).
            hg_idx: Identify processing stage: init stage or refine stage
        Return:
            tuple(Tensor): Optical flow. 'flows_forward' corresponds to the
                flows used for forward-time propagation (current to previous).
                'flows_backward' corresponds to the flows used for
                backward-time propagation (current to next).
        """

        n, t, c, h, w = guides.size() ## same resolution as final output
        guides_1 = guides[:, :-1, :, :, :]
        guides_2 = guides[:, 1:, :, :, :]
        
        if self.cpu_cache:
            flows_backward = []
            for tt in range(t-1):
                fb = self.spynet[f"hg_{hg_idx}"](guides_1[:,tt], guides_2[:,tt])
                flows_backward.append(fb.unsqueeze(1))
            flows_backward = torch.cat(flows_backward, dim = 1)
        
        else:
            guides_1 = guides_1.reshape(-1, c, h, w)
            guides_2 = guides_2.reshape(-1, c, h, w)
            flows_backward = self.spynet[f"hg_{hg_idx}"](guides_1, guides_2).view(
                n, t - 1, 2, h, w
            )
        
        if self.is_mirror_extended:  # flows_forward = flows_backward.flip(1)
            flows_forward = None
        else:
            
            if self.cpu_cache:
                flows_forward = []
                for tt in range(t-1):
                    ff = self.spynet[f"hg_{hg_idx}"](guides_2[:,tt], guides_1[:,tt])
                    flows_forward.append(ff.unsqueeze(1))
                flows_forward = torch.cat(flows_forward, dim = 1)

            else:
                guides_1 = guides_1.reshape(-1, c, h, w)
                guides_2 = guides_2.reshape(-1, c, h, w)
                flows_forward = self.spynet[f"hg_{hg_idx}"](guides_2, guides_1).view(
                    n, t - 1, 2, h, w
                )
        
        if self.cpu_cache:
            flows_backward = flows_backward.cpu()
            flows_forward = flows_forward.cpu()

        return flows_forward, flows_backward

    def propagate(self, feats, flows, module_name, hg_idx):
        """Propagate the latent features throughout the sequence.
        Args:
            feats dict(list[tensor]): Features from previous branches. Each
                component is a list of tensors with shape (n, c, h/4, w/4).
            flows (tensor): Optical flows with shape (n, t - 1, 2, h/4, w/4).
            module_name (str): The name of the propagation branches. Can either
                be 'backward_1', 'forward_1', 'backward_2', 'forward_2'.
            hg_idx: Identify processing stage: init stage or refine stage
        Return:
            dict(list[tensor]): A dictionary containing all the propagated
                features. Each key in the dictionary corresponds to a
                propagation branch, which is represented by a list of tensors.
        """

        n, t, _, h, w = flows.size() ## 1/4 resolution of final output

        frame_idx = range(0, t + 1)
        flow_idx = range(-1, t)
        mapping_idx = list(range(0, len(feats["spatial"])))
        mapping_idx += mapping_idx[::-1]

        if "backward" in module_name:
            frame_idx = frame_idx[::-1]
            flow_idx = frame_idx

        feat_prop = flows.new_zeros(n, self.mid_channels, h, w)
        for i, idx in enumerate(frame_idx):
            feat_current = feats["spatial"][mapping_idx[idx]]
            if self.cpu_cache:
                feat_current = feat_current.cuda()
                feat_prop = feat_prop.cuda()
            # second-order deformable alignment
            if i > 0:
                flow_n1 = flows[:, flow_idx[i], :, :, :]
                if self.cpu_cache:
                    flow_n1 = flow_n1.cuda()

                cond_n1 = flow_warp(feat_prop, flow_n1.permute(0, 2, 3, 1))

                # initialize second-order features
                feat_n2 = torch.zeros_like(feat_prop)
                flow_n2 = torch.zeros_like(flow_n1)
                cond_n2 = torch.zeros_like(cond_n1)

                if i > 1:  # second-order features
                    feat_n2 = feats[module_name][-2]
                    if self.cpu_cache:
                        feat_n2 = feat_n2.cuda()

                    flow_n2 = flows[:, flow_idx[i - 1], :, :, :]
                    if self.cpu_cache:
                        flow_n2 = flow_n2.cuda()

                    flow_n2 = flow_n1 + flow_warp(flow_n2, flow_n1.permute(0, 2, 3, 1))
                    cond_n2 = flow_warp(feat_n2, flow_n2.permute(0, 2, 3, 1))

                # flow-guided deformable convolution
                cond = torch.cat([cond_n1, feat_current, cond_n2], dim=1)
                feat_prop = torch.cat([feat_prop, feat_n2], dim=1)
                feat_prop = self.deform_align[f"hg_{hg_idx}"][module_name](
                    feat_prop, cond, flow_n1, flow_n2
                )

            # concatenate and residual blocks

            feat = (
                [feat_current]
                + [feats[k][idx] for k in feats if k not in ["spatial", module_name]]
                + [feat_prop]
            )
            if self.cpu_cache:
                feat = [f.cuda() for f in feat]

            feat = torch.cat(feat, dim=1)
            feat_prop = feat_prop + self.backbone[f"hg_{hg_idx}"][module_name](feat)
            feats[module_name].append(feat_prop)

            if self.cpu_cache:
                feats[module_name][-1] = feats[module_name][-1].cpu()
                torch.cuda.empty_cache()

        if "backward" in module_name:
            feats[module_name] = feats[module_name][::-1]

        return feats

    def upsample(self, lqs, feats, hg_idx):
        """Compute the output image given the features.
        Args:
            lqs (tensor): Input low quality (LQ) sequence with
                shape (n, t, c, h/s, w/s).
            feats (dict): The features from the propagation branches.
            hg_idx: Identify processing stage: init stage or refine stage
        Returns:
            Tensor: Output HR sequence with shape (n, t, c, h, w).
        """

        depths = []
        confs = []
        feats_fused = []
        num_outputs = len(feats["spatial"])

        mapping_idx = list(range(0, num_outputs))
        mapping_idx += mapping_idx[::-1]

        for i in range(0, lqs.size(1)):
            hr = [feats[k].pop(0) for k in feats if k != "spatial"]
            hr.insert(0, feats["spatial"][mapping_idx[i]])
            hr = torch.cat(hr, dim=1)
            if self.cpu_cache:
                hr = hr.cuda()

            hr = self.reconstruction[f"hg_{hg_idx}"](hr)
            feat_fused = hr.clone()
            hr = self.final_pred[f"hg_{hg_idx}"](hr)

            depth, conf = torch.chunk(hr, 2, dim=1)
            depth = depth + self.img_upsample(lqs[:, i, :, :, :])
            if self.cpu_cache:
                hr = hr.cpu()
                depth = depth.cpu()
                conf = conf.cpu()
                torch.cuda.empty_cache()

            depths.append(depth)
            confs.append(conf)
            feats_fused.append(feat_fused)

        return (
            torch.stack(depths, dim=1),
            torch.stack(confs, dim=1),
            torch.stack(feats_fused, dim=1),
        )

    def hg_forward(self, lqs, guides, extra_inputs=None, extra_feats=None, hg_idx=1):
        """Forward function for a single stage (Two stages in total).
        Args:
            lqs (tensor): Input low quality (LQ) sequence with
                shape (n, t, 1, h/4, w/4).
            guides (tensor): Input RGB guidance with shape (n, t, 3, h, w)
            extra_inputs (tensor): if in the second stage, also takes in
                depth and confidence predictions from the first stage
            extra_feats (tensor): if in the second stage, also takes in
                features from the first stage
            hg_idx: Identify processing stage: init stage or refine stage
        Return:
            depth (tensor): current stage depth prediction with shape (n, t, 1, h, w)
            conf (tensor): current stage confidence prediction with shape (n, t, 1, h, w)
            feats_fused (tensor): if in the first stage, also return features
        """

        n, t, c, h, w = lqs.size() ## 1/4 resolution of final output

        # whether to cache the features in CPU (no effect if using CPU)
        if t > self.cpu_cache_length and lqs.is_cuda:
            self.cpu_cache = True
        else:
            self.cpu_cache = False

        # check whether the input is an extended sequence
        self.check_if_mirror_extended(lqs)

        feats = {}
        # compute spatial features
        if self.cpu_cache:
            feats["spatial"] = []
            for i in range(0, t):
                if hg_idx == 1:
                    guide_feat = self.conv_guide_init[f"hg_{hg_idx}"](
                        guides[:, i, :, :, :]
                    )
                    feat = self.feat_extract[f"hg_{hg_idx}"](
                        torch.cat([lqs[:, i, :, :, :], guide_feat], dim=1)
                    ).cpu()
                else:
                    guide_feat = self.conv_guide_init[f"hg_{hg_idx}"](
                        extra_inputs[:, i, :, :, :].to(guides.device)
                    )
                    feat = self.feat_extract[f"hg_{hg_idx}"](
                        torch.cat(
                            [
                                lqs[:, i, :, :, :],
                                guide_feat,
                                extra_feats[:, i, :, :, :],
                            ],
                            dim=1,
                        )
                    ).cpu()
                feats["spatial"].append(feat)
                torch.cuda.empty_cache()
        else:
            if hg_idx == 1:
                guide_feats_ = self.conv_guide_init[f"hg_{hg_idx}"](
                    guides.view(-1, 3, int(h * 4), int(w * 4))
                )
                feats_ = self.feat_extract[f"hg_{hg_idx}"](
                    torch.cat(
                        [
                            lqs.view(-1, c, h, w),
                            guide_feats_,
                        ],
                        dim=1,
                    )
                )
            else:
                guide_feats_ = self.conv_guide_init[f"hg_{hg_idx}"](
                    extra_inputs.view(-1, 2, int(h * 4), int(w * 4))
                )
                feats_ = self.feat_extract[f"hg_{hg_idx}"](
                    torch.cat(
                        [
                            lqs.view(-1, c, h, w),
                            guide_feats_,
                            extra_feats.view(-1, self.mid_channels, h, w),
                        ],
                        dim=1,
                    )
                )
            h, w = feats_.shape[2:]
            feats_ = feats_.view(n, t, -1, h, w)
            feats["spatial"] = [feats_[:, i, :, :, :] for i in range(0, t)]
        
        # compute optical flow using the low-res inputs
        flows_forward, flows_backward = self.compute_flow(guides, hg_idx)
        
        flows_forward = (
            F.interpolate(
                flows_forward.view(-1, 2, int(h * 4), int(w * 4)),
                scale_factor=0.25,
                mode="bicubic",
            ).view(n, t - 1, 2, h, w)
            / 4
        )
        flows_backward = (
            F.interpolate(
                flows_backward.view(-1, 2, int(h * 4), int(w * 4)),
                scale_factor=0.25,
                mode="bicubic",
            ).view(n, t - 1, 2, h, w)
            / 4
        )
        
        # feature propagation
        for iter_ in [1, 2]:
            for direction in ["backward", "forward"]:
                module = f"{direction}_{iter_}"

                feats[module] = []

                if direction == "backward":
                    flows = flows_backward
                elif flows_forward is not None:
                    flows = flows_forward
                else:
                    flows = flows_backward.flip(1)

                feats = self.propagate(feats, flows, module, hg_idx)
                if self.cpu_cache:
                    del flows
                    torch.cuda.empty_cache()
        depth, conf, feats_fused = self.upsample(lqs, feats, hg_idx)
        if hg_idx == 1:
            return depth, conf, feats_fused
        else:
            return depth, conf, None

    def forward(self, lqs, guides):
        """Forward function for BasicVSR++.
        Args:
            lqs (tensor): Input low quality (LQ) sequence with
                shape (n, t, 1, h/s, w/s).
            guides (tensor): Input RGB guidance with shape (n, t, 3, h, w)
        Returns:
            Tensor: Output HR sequence with shape (n, t, c, h, w).
        """
        lqs = lqs.repeat_interleave(self.scale//4, dim = 3).repeat_interleave(self.scale//4, dim = 4)
        n, t, c, h, w = lqs.size() ## 1/4 resolution of final output

        rgb_depth, rgb_conf, rgb_feats = self.hg_forward(lqs, guides, hg_idx=1)
        d_depth, d_conf, _ = self.hg_forward(
            lqs, guides, torch.cat((rgb_depth, rgb_conf), dim=2), rgb_feats, hg_idx=2
        )

        rgb_conf, d_conf = torch.chunk(
            self.softmax(
                torch.cat(
                    (
                        rgb_conf,
                        d_conf,
                    ),
                    dim=2,
                )
            ),
            2,
            dim=2,
        )
        
        depth_final = d_depth * d_conf + rgb_depth * rgb_conf
        intermed = {
            "d_depth": d_depth,
            "rgb_depth": rgb_depth,
            "d_conf": d_conf,
            "rgb_conf": rgb_conf,
        }
        return depth_final, intermed

    def init_weights(self, pretrained=None, strict=True):
        """Init weights for models.
        Args:
            pretrained (str, optional): Path for pretrained weights. If given
                None, pretrained weights will not be loaded. Default: None.
            strict (bool, optional): Whether strictly load the pretrained
                model. Default: True.
        """
        if isinstance(pretrained, str):
            load_checkpoint(self, pretrained, strict=strict)
        elif pretrained is not None:
            raise TypeError(
                f'"pretrained" must be a str or None. '
                f"But received {type(pretrained)}."
            )
