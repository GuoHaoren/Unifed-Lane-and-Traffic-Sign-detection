import math
from typing import List, Dict
import torch
from torch import nn
from torch.nn import functional as F
from .lane_detection import parsingNet
from detectron2.layers import ShapeSpec, NaiveSyncBatchNorm
from detectron2.modeling.proposal_generator.build import PROPOSAL_GENERATOR_REGISTRY
import numpy as np
from adet.layers import DFConv2d, NaiveGroupNorm
from adet.utils.comm import compute_locations
from .fcos_outputs import FCOSOutputs
from ..ultra_fast.factory import get_loss_dict
from ..ultra_fast.cal_loss import inference,calc_loss
#import pdb
__all__ = ["FCOS"]

INF = 100000000


class Scale(nn.Module):
    def __init__(self, init_value=1.0):
        super(Scale, self).__init__()
        self.scale = nn.Parameter(torch.FloatTensor([init_value]))

    def forward(self, input):
        return input * self.scale


class ModuleListDial(nn.ModuleList):
    def __init__(self, modules=None):
        super(ModuleListDial, self).__init__(modules)
        self.cur_position = 0

    def forward(self, x):
        result = self[self.cur_position](x)
        self.cur_position += 1
        if self.cur_position >= len(self):
            self.cur_position = 0
        return result


@PROPOSAL_GENERATOR_REGISTRY.register()
class FCOS(nn.Module):
    """
    Implement FCOS (https://arxiv.org/abs/1904.01355).
    """
    def __init__(self, cfg, input_shape: Dict[str, ShapeSpec]):
        super().__init__()
      
        self.in_features = cfg.MODEL.FCOS.IN_FEATURES
        self.fpn_strides = cfg.MODEL.FCOS.FPN_STRIDES
        self.yield_proposal = cfg.MODEL.FCOS.YIELD_PROPOSAL

        self.fcos_head = FCOSHead(cfg, [input_shape[f] for f in self.in_features])
      
        # if self.training:
        self.lane_head = parsingNet().cuda()
        # else:
            
        #     self.lane_head = parsingNet(use_aux=False).cuda()

        self.in_channels_to_top_module = self.fcos_head.in_channels_to_top_module
        self.fcos_outputs = FCOSOutputs(cfg)

  
    def forward_head(self, features, top_module=None):
        features = [features[f] for f in self.in_features]
        pred_class_logits, pred_deltas, pred_centerness, top_feats, bbox_towers = self.fcos_head(
            features, top_module, self.yield_proposal)
        return pred_class_logits, pred_deltas, pred_centerness, top_feats, bbox_towers

    def forward(self, images, features, gt_instances=None, top_module=None, lane_detection = False, use_ax=True,gt_segmentation = [], idx = [], ori = None):
        """
        Arguments:
            images (list[Tensor] or ImageList): images to be processed
            targets (list[BoxList]): ground-truth boxes present in the image (optional)

        Returns:
            result (list[BoxList] or dict[Tensor]): the output from the model.
                During training, it returns a dict[Tensor] which contains the losses.
                During testing, it returns list[BoxList] contains additional fields
                like `scores`, `labels` and `mask` (for Mask R-CNN models).

        """
    
        #import pdb
    
    
        features = [features[f] for f in self.in_features]

        features_tmp = []



        if self.training:

            for j in range(0, len(features)):
                # for i in idx:
                extra = features[j][idx,:,:]
                #extra = torch.tensor(extra)
                features_tmp.append(extra)
        else:
            features_tmp = features
        
        #print("lane detection", lane_detection)
        #print("size of features", features_tmp[0].size())
        #cls_loss = 0

        if lane_detection or not self.training:
        #if lane_detection:
            #if(features_tmp[0][0].size() != torch.Size([256,60,100])):
                #return 0
            
            if self.training:

               
                # lane_net = parsingNet(in_model = features_tmp,use_aux=use_ax).cuda()
                cls_loss,seg_loss = self.lane_head.forward(features = features_tmp,aux=True)

            

                loss_dict = get_loss_dict(use_ax)#need to set up and pass config file, currelenntly using simplified version
                #lane_loss_to = 0.0
                
                # import pdb; pdb.set_trace()
            
                gt_instances = torch.stack(gt_instances).cuda()
                # print('hahahahahahahahahah')
                gt_segmentation = torch.stack(gt_segmentation).cuda()
                # for no in range(len(gt_instances)): 
                # results = inference(gt_instances[no],gt_segmentation[no],use_ax,cls_out=cls_loss[no],seg_out=seg_loss[no])
                results = inference(gt_instances,gt_segmentation,use_ax,cls_out=cls_loss,seg_out=seg_loss)

                # pdb.set_trace()
                lane_loss = calc_loss(loss_dict,results=results)
                #lane_loss_to+=lane_loss

                # return lane_loss_to/len(gt_instances)
                
                

                return lane_loss

            #cls_loss = parsingNet(features = features_tmp,use_aux=False).cuda()
            # self.lane_head = parsingNet(use_aux=False).cuda()
            cls_loss = self.lane_head.forward(features=features_tmp,aux=False)
  

            
        if not lane_detection or not self.training:   
            
            
            locations = self.compute_locations(features_tmp)
            logits_pred, reg_pred, ctrness_pred, top_feats, bbox_towers = self.fcos_head(
                features_tmp, top_module, self.yield_proposal
            )

      
   
            results = {}
            if self.yield_proposal:
                results["features"] = {
                    f: b for f, b in zip(self.in_features, bbox_towers)
                }

            if self.training:
                results, losses = self.fcos_outputs.losses(
                    logits_pred, reg_pred, ctrness_pred,
                    locations, gt_instances, top_feats
                )
                
                if self.yield_proposal:
                    with torch.no_grad():
                        results["proposals"] = self.fcos_outputs.predict_proposals(
                            logits_pred, reg_pred, ctrness_pred,
                            locations, images.image_sizes, top_feats
                        )
                # losses['lane_loss'] = lane_loss

                return results, losses
            
            results = self.fcos_outputs.predict_proposals(
                logits_pred, reg_pred, ctrness_pred,
                locations, images.image_sizes, top_feats
            )

 
    

            return results, cls_loss

    def compute_locations(self, features):
        locations = []
        for level, feature in enumerate(features):
            h, w = feature.size()[-2:]
            locations_per_level = compute_locations(
                h, w, self.fpn_strides[level],
                feature.device
            )
            locations.append(locations_per_level)
        return locations


class FCOSHead(nn.Module):
    def __init__(self, cfg, input_shape: List[ShapeSpec]):
        """
        Arguments:
            in_channels (int): number of channels of the input feature
        """
        super().__init__()
        # TODO: Implement the sigmoid version first.
        self.num_classes = cfg.MODEL.FCOS.NUM_CLASSES
        self.fpn_strides = cfg.MODEL.FCOS.FPN_STRIDES
        head_configs = {"cls": (cfg.MODEL.FCOS.NUM_CLS_CONVS,
                                cfg.MODEL.FCOS.USE_DEFORMABLE),
                        "bbox": (cfg.MODEL.FCOS.NUM_BOX_CONVS,
                                 cfg.MODEL.FCOS.USE_DEFORMABLE),
                        "share": (cfg.MODEL.FCOS.NUM_SHARE_CONVS,
                                  False)}
        norm = None if cfg.MODEL.FCOS.NORM == "none" else cfg.MODEL.FCOS.NORM
        self.num_levels = len(input_shape)

        in_channels = [s.channels for s in input_shape]
        assert len(set(in_channels)) == 1, "Each level must have the same channel!"
        in_channels = in_channels[0]

        self.in_channels_to_top_module = in_channels

        for head in head_configs:
            tower = []
            num_convs, use_deformable = head_configs[head]
            for i in range(num_convs):
                if use_deformable and i == num_convs - 1:
                    conv_func = DFConv2d
                else:
                    conv_func = nn.Conv2d
                tower.append(conv_func(
                    in_channels, in_channels,
                    kernel_size=3, stride=1,
                    padding=1, bias=True
                ))
                if norm == "GN":
                    tower.append(nn.GroupNorm(32, in_channels))
                elif norm == "NaiveGN":
                    tower.append(NaiveGroupNorm(32, in_channels))
                elif norm == "BN":
                    tower.append(ModuleListDial([
                        nn.BatchNorm2d(in_channels) for _ in range(self.num_levels)
                    ]))
                elif norm == "SyncBN":
                    tower.append(ModuleListDial([
                        NaiveSyncBatchNorm(in_channels) for _ in range(self.num_levels)
                    ]))
                tower.append(nn.ReLU())
            self.add_module('{}_tower'.format(head),
                            nn.Sequential(*tower))

        self.cls_logits = nn.Conv2d(
            in_channels, self.num_classes,
            kernel_size=3, stride=1,
            padding=1
        )
        self.bbox_pred = nn.Conv2d(
            in_channels, 4, kernel_size=3,
            stride=1, padding=1
        )
        self.ctrness = nn.Conv2d(
            in_channels, 1, kernel_size=3,
            stride=1, padding=1
        )

        if cfg.MODEL.FCOS.USE_SCALE:
            self.scales = nn.ModuleList([Scale(init_value=1.0) for _ in range(self.num_levels)])
        else:
            self.scales = None

        for modules in [
            self.cls_tower, self.bbox_tower,
            self.share_tower, self.cls_logits,
            self.bbox_pred, self.ctrness
        ]:
            for l in modules.modules():
                if isinstance(l, nn.Conv2d):
                    torch.nn.init.normal_(l.weight, std=0.01)
                    torch.nn.init.constant_(l.bias, 0)

        # initialize the bias for focal loss
        prior_prob = cfg.MODEL.FCOS.PRIOR_PROB
        bias_value = -math.log((1 - prior_prob) / prior_prob)
        torch.nn.init.constant_(self.cls_logits.bias, bias_value)

    def forward(self, x, top_module=None, yield_bbox_towers=False):
        logits = []
        bbox_reg = []
        ctrness = []
        top_feats = []
        bbox_towers = []
        for l, feature in enumerate(x):
            feature = self.share_tower(feature)
            cls_tower = self.cls_tower(feature)
            bbox_tower = self.bbox_tower(feature)
            if yield_bbox_towers:
                bbox_towers.append(bbox_tower)

            logits.append(self.cls_logits(cls_tower))
            ctrness.append(self.ctrness(bbox_tower))
            reg = self.bbox_pred(bbox_tower)
            if self.scales is not None:
                reg = self.scales[l](reg)
            # Note that we use relu, as in the improved FCOS, instead of exp.
            bbox_reg.append(F.relu(reg))
            if top_module is not None:
                top_feats.append(top_module(bbox_tower))

        return logits, bbox_reg, ctrness, top_feats, bbox_towers