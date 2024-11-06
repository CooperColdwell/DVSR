HVSR Implementation with Detailed Comments

# Copyright (c) Meta Platforms, Inc. and affiliates.
# Adapted from BasicVSR++ network structure: "BasicVSR++: Improving Video Super-Resolution with Enhanced Propagation and Alignment"

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from mmcv.cnn import constant_init
from mmcv.ops import ModulatedDeformConv2d, modulated_deform_conv2d
from mmcv.runner import load_checkpoint

from .common import PixelShufflePack, flow_warp, ResidualBlocksWithInputConv, SPyNet, SecondOrderDeformableAlignment
from .registry import BACKBONES
from mmseg.utils import get_root_logger


def dtof_hist_torch(d, img, rebin_idx, pitch, temp_res):
    """
    Convert predicted depth map into a histogram for comparison with dToF sensor data
    
    Args:
        d (tensor): predicted depth map with size (n*t, 1, h, w)
        img (tensor): input guidance image with size (n*t, 3, h, w)
        rebin_idx (tensor):
            Compression rebin index (see 'datasets/dtof_simulator.py' for details)
            with size (n*t, 2*self.mpeaks+2, h/s, w/s)
        pitch: size of each patch (iFoV), same as self.scale in main model
        temp_res: temporal resolution of dToF sensor
    """
    d = torch.clamp(d.clone(), min=0.0, max=1.0).to(img.device)
    B, _, H, W = d.shape ## same resolution as final output
    _, M, _, _ = rebin_idx.shape
    
    # Calculate albedo from input image
    albedo = torch.mean(img, dim=1).unsqueeze(1)
    # Apply inverse square law for intensity falloff
    r = albedo / (1e-3 + d**2)

    # Upsample rebin indices to match depth map resolution
    rebin_idx = torch.repeat_interleave(
        torch.repeat_interleave(rebin_idx, pitch, dim=2), pitch, dim=3
    ).detach()
    
    # Create histogram by comparing depth values with rebin indices
    hist = torch.sum(
        ((torch.round(d * (temp_res - 1)) - rebin_idx) >= 0).float(), dim=1
    ).unsqueeze(1)

    # Convert to one-hot representation
    idx_volume = (
        torch.arange(1, M + 1).unsqueeze(0).unsqueeze(2).unsqueeze(3).float().to(img.device)
    )
    hist = ((hist - idx_volume) == 0).float()
    
    # Apply intensity weighting and downsample
    hist = torch.sum(
        (hist * r).view(B, M, H // pitch, pitch, W // pitch, pitch), dim=(3, 5)
    )
    return hist


def get_inp_error(cdf, rebin_idx, pred, img, pitch, temp_res):
    """
    Calculate histogram matching error between predicted depth and sensor measurements
    
    Args:
        cdf (tensor): Input compressed cumulative distribution functions (n*t, 2*mpeaks+2, h/s, w/s)
        rebin_idx (tensor): Compression rebin indices (n*t, 2*self.mpeaks+2, h/s, w/s)
        pred (tensor): Predicted depth map (n*t, 1, h, w)
        img (tensor): Input guidance RGB image (n*t, 3, h, w)
        pitch: Size of each patch (iFoV), same as self.scale in main model
        temp_res: Temporal resolution
        
    Returns:
        tensor: Histogram matching error map
    """
    B, M, h, w = rebin_idx.shape
    delta_idx = rebin_idx[:, 1:] - rebin_idx[:, :-1]

    # Normalize input CDFs
    cdf_inp = cdf / (torch.max(cdf, dim=1)[0].unsqueeze(1) + 1e-3)

    # Generate histogram from prediction and convert to CDF
    hist_pred = dtof_hist_torch(pred, img, rebin_idx[:, :-1], pitch, temp_res)
    hist_pred = hist_pred / (torch.sum(hist_pred, dim=1).unsqueeze(1) + 1e-3)
    cdf_pred = torch.cumsum(hist_pred, dim=1).detach()
    
    # Calculate error between CDFs
    inp_error = torch.mean(
        torch.abs((cdf_inp[:, 1:] - cdf_pred) * delta_idx), dim=1
    ).unsqueeze(1)
    inp_error[torch.max(cdf_inp, axis=1)[0].unsqueeze(1) == 0] = -1

    # Upsample error map
    inp_error = torch.repeat_interleave(
        torch.repeat_interleave(inp_error, pitch, dim=2), pitch, dim=3
    ).detach()
    del hist_pred, cdf_pred, cdf_inp, rebin_idx
    return inp_error


def get_pos_encoding(B, T, H, W, pitch):
    """
    Generate positional encodings to assist alignment vector predictions
    
    Args:
        B, T, H, W: Batch size, number of frames, height and width of sequence
        pitch: Size of each patch (iFoV) same as self.scale in main model
        
    Returns:
        tensor: Position encodings containing absolute positions, relative positions within patches,
               and patch center positions
    """
    # Create coordinate grids
    y, x = torch.meshgrid(torch.arange(H), torch.arange(W))
    y = y.unsqueeze(0).unsqueeze(1).float()
    x = x.unsqueeze(0).unsqueeze(1).float()
    
    # Calculate patch center coordinates
    patch_y = -torch.nn.MaxPool2d(kernel_size=pitch, stride=pitch)(-y)
    patch_x = -torch.nn.MaxPool2d(kernel_size=pitch, stride=pitch)(-x)
    patch_y = torch.repeat_interleave(
        torch.repeat_interleave(patch_y, pitch, dim=2), pitch, dim=3
    )
    patch_x = torch.repeat_interleave(
        torch.repeat_interleave(patch_x, pitch, dim=2), pitch, dim=3
    )
    
    # Calculate relative positions within patches
    rel_y = y - patch_y
    rel_x = x - patch_x
    
    # Combine different position representations
    abs_pos = torch.cat((y / H, x / W), dim=1)
    rel_pos = torch.cat((rel_y / pitch, rel_x / pitch), dim=1)
    patch_pos = torch.cat((patch_y / H, patch_x / W), dim=1)
    pos_encoding = torch.cat((abs_pos, rel_pos, patch_pos), dim=1).float().unsqueeze(1)
    return pos_encoding.repeat(B, T, 1, 1, 1)


@BACKBONES.register_module()
class HVSR(nn.Module):
    """
    Hierarchical Video Super-Resolution Network for dToF Depth Enhancement
    
    The network consists of two stages:
    1. Initial depth prediction using RGB guidance
    2. Refinement using histogram matching error and positional encoding
    
    Each stage uses feature propagation with deformable alignment for temporal consistency.
    
    Args:
        dtof_args: Arguments for dToF sensor simulation
        mid_channels (int): Channel number of intermediate features
        num_blocks (int): Number of residual blocks in propagation branches
        scale (int): dToF sensor downsampling scale
        max_residue_magnitude (int): Maximum magnitude of offset residue
        is_low_res_input (bool): Whether input is low-resolution
        spynet_pretrained (str): Pre-trained model path for optical flow network
        cpu_cache_length (int): Threshold for using CPU cache to save GPU memory
    """

    def __init__(
        self,
        dtof_args,
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

        self.args = dtof_args
        self.mpeaks = self.args['mpeaks']
        self.temp_res = self.args['temp_res']

        # Optical flow networks for both stages
        self.spynet = nn.ModuleDict()
        self.spynet["hg_1"] = SPyNet(pretrained=spynet_pretrained)
        self.spynet["hg_2"] = SPyNet(pretrained=spynet_pretrained)

        # Feature extraction modules
        self.conv_guide_init = nn.ModuleDict()
        # Stage 1: Process RGB guidance
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
        # Stage 2: Process depth, confidence, position encoding and error map
        self.conv_guide_init["hg_2"] = nn.Sequential(
            nn.Conv2d(2 + 6 + 1, mid_channels, kernel_size=3, stride=1, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            nn.Conv2d(mid_channels, mid_channels, kernel_size=3, stride=2, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            ResidualBlocksWithInputConv(mid_channels, mid_channels, 1),
            nn.Conv2d(mid_channels, mid_channels, kernel_size=3, stride=2, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            ResidualBlocksWithInputConv(mid_channels, mid_channels, 2),
        )
        
        # Feature extraction after initial convolutions
        self.feat_extract = nn.ModuleDict()
        self.feat_extract["hg_1"] = ResidualBlocksWithInputConv(
            self.mpeaks + mid_channels, mid_channels, 5
        )
        self.feat_extract["hg_2"] = ResidualBlocksWithInputConv(
            self.mpeaks + mid_channels * 2, mid_channels, 5
        )

        # Propagation modules for both stages
        self.deform_align = nn.ModuleDict()
        self.deform_align["hg_1"] = nn.ModuleDict()
        self.deform_align["hg_2"] = nn.ModuleDict()
        self.backbone = nn.ModuleDict()
        self.backbone["hg_1"] = nn.ModuleDict()
        self.backbone["hg_2"] = nn.ModuleDict()

        # Initialize propagation modules for both forward and backward directions
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

        # Activation function
        self.lrelu = nn.LeakyReLU(negative_slope=0.1, inplace=True)

        # Reconstruction modules for both stages
        self.reconstruction = nn.ModuleDict()
        self.reconstruction["hg_1"] = ResidualBlocksWithInputConv(
            5 * mid_channels, mid_channels, 5
        )
        self.reconstruction["hg_2"] = ResidualBlocksWithInputConv(
            5 * mid_channels, mid_channels, 5
        )

        # Final prediction layers for depth and confidence
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

        # Upsampling and activation layers
        self.img_upsample = nn.Upsample(
            scale_factor=4, mode="bilinear", align_corners=False
        )
        self.softmax = nn.Softmax(dim=2)

        # Flag for mirror-extended sequence
        self.is_mirror_extended = False

def check_if_mirror_extended(self, lqs):
        """Check whether the input sequence is mirror-extended.
        
        Mirror extension means the sequence is reflected around its midpoint,
        where frame i equals frame (t-1-i). For example, in a 6-frame sequence:
        [0,1,2,2,1,0] is mirror-extended.
        
        Args:
            lqs (tensor): Input low quality sequence with shape (n, t, c, h, w)
                where n=batch size, t=sequence length, c=channels, h=height, w=width
        """
        # Only check if sequence length is even
        if lqs.size(1) % 2 == 0:
            # Split sequence into two equal halves
            lqs_1, lqs_2 = torch.chunk(lqs, 2, dim=1)
            # Check if first half equals second half reversed
            # If norm is 0, sequences are identical
            if torch.norm(lqs_1 - lqs_2.flip(1)) == 0:
                self.is_mirror_extended = True

    def compute_flow(self, guides, hg_idx):
        """Compute optical flow between consecutive frames using SPyNet.
        
        For mirror-extended sequences, only backward flow is computed since
        forward flow can be derived from backwards flow.
        
        Args:
            guides (tensor): Input sequence, shape (n, t, c, h, w)
            hg_idx: Stage identifier (1=initial stage, 2=refinement stage)
        
        Returns:
            tuple(Tensor): Forward and backward optical flows:
                - flows_forward: Flow from current to previous frame
                - flows_backward: Flow from current to next frame
                Both have shape (n, t-1, 2, h, w)
        """
        n, t, c, h, w = guides.size()
        # Split into consecutive pairs for flow computation 
        guides_1 = guides[:, :-1, :, :, :]  # frames 0 to t-1
        guides_2 = guides[:, 1:, :, :, :]   # frames 1 to t
        
        if self.cpu_cache:
            # Process frames sequentially when using CPU cache
            flows_backward = []
            for tt in range(t-1):
                fb = self.spynet[f"hg_{hg_idx}"](guides_1[:,tt], guides_2[:,tt])
                flows_backward.append(fb.unsqueeze(1))
            flows_backward = torch.cat(flows_backward, dim = 1)
        
        else:
            # Process all frames at once if memory allows
            guides_1 = guides_1.reshape(-1, c, h, w)
            guides_2 = guides_2.reshape(-1, c, h, w)
            flows_backward = self.spynet[f"hg_{hg_idx}"](guides_1, guides_2).view(
                n, t - 1, 2, h, w
            )
        
        # For mirror-extended sequences, forward flow is backward flow reversed
        if self.is_mirror_extended:
            flows_forward = None
        else:
            if self.cpu_cache:
                # Sequential processing for forward flows
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
        
        # Move flows to CPU if using CPU cache to save GPU memory
        if self.cpu_cache:
            flows_backward = flows_backward.cpu()
            flows_forward = flows_forward.cpu()

        return flows_forward, flows_backward

    def propagate(self, feats, flows, module_name, hg_idx):
        """Propagate features through the sequence using deformable convolution.
        
        Implements bi-directional feature propagation with second-order motion
        compensation using deformable convolution.
        
        Args:
            feats dict(list[tensor]): Previous branch features
                Each key maps to list of tensors with shape (n, c, h/4, w/4)
            flows (tensor): Optical flows, shape (n, t-1, 2, h/4, w/4) 
            module_name (str): Branch identifier:
                'backward_1', 'forward_1', 'backward_2', or 'forward_2'
            hg_idx: Stage identifier (1=initial, 2=refinement)
            
        Returns:
            dict(list[tensor]): Updated feature dictionary with propagated features
        """
        n, t, _, h, w = flows.size()

        # Generate frame indices for propagation
        frame_idx = range(0, t + 1)
        flow_idx = range(-1, t)
        # Create mapping for handling mirror-extended sequences
        mapping_idx = list(range(0, len(feats["spatial"])))
        mapping_idx += mapping_idx[::-1]

        # Reverse indices for backward propagation
        if "backward" in module_name:
            frame_idx = frame_idx[::-1]
            flow_idx = frame_idx

        # Initialize feature propagation tensor
        feat_prop = flows.new_zeros(n, self.mid_channels, h, w)
        
        # Main propagation loop
        for i, idx in enumerate(frame_idx):
            feat_current = feats["spatial"][mapping_idx[idx]]
            if self.cpu_cache:
                feat_current = feat_current.cuda()
                feat_prop = feat_prop.cuda()
                
            # Apply second-order deformable alignment after first frame
            if i > 0:
                # Get first-order flow and features
                flow_n1 = flows[:, flow_idx[i], :, :, :]
                if self.cpu_cache:
                    flow_n1 = flow_n1.cuda()
                cond_n1 = flow_warp(feat_prop, flow_n1.permute(0, 2, 3, 1))

                # Initialize second-order terms
                feat_n2 = torch.zeros_like(feat_prop)
                flow_n2 = torch.zeros_like(flow_n1)
                cond_n2 = torch.zeros_like(cond_n1)

                # Compute second-order terms if available
                if i > 1:
                    feat_n2 = feats[module_name][-2]
                    if self.cpu_cache:
                        feat_n2 = feat_n2.cuda()

                    flow_n2 = flows[:, flow_idx[i - 1], :, :, :]
                    if self.cpu_cache:
                        flow_n2 = flow_n2.cuda()

                    # Compose flows for second-order motion
                    flow_n2 = flow_n1 + flow_warp(flow_n2, flow_n1.permute(0, 2, 3, 1))
                    cond_n2 = flow_warp(feat_n2, flow_n2.permute(0, 2, 3, 1))

                # Concatenate features for deformable alignment
                cond = torch.cat([cond_n1, feat_current, cond_n2], dim=1)
                feat_prop = torch.cat([feat_prop, feat_n2], dim=1)
                feat_prop = self.deform_align[f"hg_{hg_idx}"][module_name](
                    feat_prop, cond, flow_n1, flow_n2
                )

            # Aggregate features and apply residual learning
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

            # Move features to CPU if using CPU cache
            if self.cpu_cache:
                feats[module_name][-1] = feats[module_name][-1].cpu()
                torch.cuda.empty_cache()

        # Reverse feature order for backward propagation
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
            depth = depth + self.img_upsample(lqs[:, i, :1, :, :])
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
                depth and confidence predictions, plus histogram error map
                and positional encodings from the first stage
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
                        extra_inputs[:, i, :, :, :]
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
                    extra_inputs.view(-1, 2 + 6 + 1, int(h * 4), int(w * 4)).to(guides.device)
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

    def forward(self, lqs_comb, guides):
        """Forward function for BasicVSR++.
        Args:
            lqs_comb (tensor): Input low quality (LQ) histogram sequence with
                shape (n, t, c, h/s, w/s).
                Peaks: (n, t, self.mpeaks, h/s, w/s)
                Compressed CDFs: (n, t, 2*self.mpeaks+2, h/s, w/s)
                Compression rebin index (see 'datasets/dtof_simulator.py' for details):
                    (n, t, 2*self.mpeaks+2, h/s, w/s)
            guides (tensor): Input RGB guidance with shape (n, t, 3, h, w)
        Returns:
            Tensor: Output HR sequence with shape (n, t, c, h, w).
        """
        mpeaks = lqs_comb[:,:,:self.mpeaks]
        cdfs = lqs_comb[:,:,self.mpeaks:(3*self.mpeaks + 3)]
        rebins = lqs_comb[:,:,(3*self.mpeaks + 3):]

        lqs = mpeaks / (self.temp_res - 1)
        n, t, c, h, w = lqs.size() ## 1/scale (default 1/16) resolution of final output
        lqs = lqs.repeat_interleave(self.scale//4, dim = 3).repeat_interleave(self.scale//4, dim = 4)

        rgb_depth, rgb_conf, rgb_feats = self.hg_forward(lqs, guides, hg_idx=1)
        
        if self.cpu_cache:
            rgb_depth = rgb_depth.to(guides.device)
            rgb_conf = rgb_conf.to(guides.device)
            
        inp_error = get_inp_error(
            cdfs.view(n * t, cdfs.shape[2], h, w),
            rebins.view(n * t, rebins.shape[2], h, w),
            rgb_depth.view(n * t, rgb_depth.shape[2], h * self.scale, w * self.scale),
            guides.view(n * t, guides.shape[2], h * self.scale, w * self.scale),
            pitch=self.scale,
            temp_res=self.temp_res,
        )
        inp_error = inp_error.view(n, t, 1, h * self.scale, w * self.scale)
        B, T, _, H, W = inp_error.shape
        pos_encoding = (
            get_pos_encoding(B, T, H, W, self.scale)
            .float()
            .detach()
            .to(inp_error.device)
        )
        
        d_depth, d_conf, _ = self.hg_forward(
            lqs,
            guides,
            torch.cat((rgb_depth, rgb_conf, pos_encoding, inp_error), dim=2),
            rgb_feats,
            hg_idx=2,
        )

        if self.cpu_cache:
            d_depth = d_depth.to(guides.device)
            d_conf = d_conf.to(guides.device)
        
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
