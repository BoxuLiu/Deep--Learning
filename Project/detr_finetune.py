# -*- coding: utf-8 -*-
"""Detr_finetune.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1fBQxmlom0pKVB2Sh5O6NCRhkNbga8qJt
"""

import torch
from torch import Tensor
import torch.nn.functional as F
import torchvision.transforms.functional as Fun
from torch import nn
from scipy.optimize import linear_sum_assignment
from typing import Optional, List
from packaging import version
import torch, torchvision
import torchvision.transforms as T
if version.parse(torchvision.__version__) < version.parse('0.7'):
    from torchvision.ops import _new_empty_tensor
    from torchvision.ops.misc import _output_size
import torch.distributed as dist
from torchvision.ops.boxes import box_area

class NestedTensor(object):
    def __init__(self, tensors, mask: Optional[Tensor]):
        self.tensors = tensors
        self.mask = mask

    def to(self, device):
        cast_tensor = self.tensors.to(device)
        mask = self.mask
        if mask is not None:
            assert mask is not None
            cast_mask = mask.to(device)
        else:
            cast_mask = None
        return NestedTensor(cast_tensor, cast_mask)

    def decompose(self):
        return self.tensors, self.mask

    def __repr__(self):
        return str(self.tensors)
@torch.no_grad()
def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    if target.numel() == 0:
        return [torch.zeros([], device=output.device)]
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res

@torch.jit.unused
def _onnx_nested_tensor_from_tensor_list(tensor_list: List[Tensor]) -> NestedTensor:
    max_size = []
    for i in range(tensor_list[0].dim()):
        max_size_i = torch.max(torch.stack([img.shape[i] for img in tensor_list]).to(torch.float32)).to(torch.int64)
        max_size.append(max_size_i)
    max_size = tuple(max_size)

    # work around for
    # pad_img[: img.shape[0], : img.shape[1], : img.shape[2]].copy_(img)
    # m[: img.shape[1], :img.shape[2]] = False
    # which is not yet supported in onnx
    padded_imgs = []
    padded_masks = []
    for img in tensor_list:
        padding = [(s1 - s2) for s1, s2 in zip(max_size, tuple(img.shape))]
        padded_img = torch.nn.functional.pad(img, (0, padding[2], 0, padding[1], 0, padding[0]))
        padded_imgs.append(padded_img)

        m = torch.zeros_like(img[0], dtype=torch.int, device=img.device)
        padded_mask = torch.nn.functional.pad(m, (0, padding[2], 0, padding[1]), "constant", 1)
        padded_masks.append(padded_mask.to(torch.bool))

    tensor = torch.stack(padded_imgs)
    mask = torch.stack(padded_masks)

    return NestedTensor(tensor, mask=mask)

def _max_by_axis(the_list):
    # type: (List[List[int]]) -> List[int]
    maxes = the_list[0]
    for sublist in the_list[1:]:
        for index, item in enumerate(sublist):
            maxes[index] = max(maxes[index], item)
    return maxes

def nested_tensor_from_tensor_list(tensor_list: List[Tensor]):
    # TODO make this more general
    if tensor_list[0].ndim == 3:
        if torchvision._is_tracing():
            # nested_tensor_from_tensor_list() does not export well to ONNX
            # call _onnx_nested_tensor_from_tensor_list() instead
            return _onnx_nested_tensor_from_tensor_list(tensor_list)

        # TODO make it support different-sized images
        max_size = _max_by_axis([list(img.shape) for img in tensor_list])
        # min_size = tuple(min(s) for s in zip(*[img.shape for img in tensor_list]))
        batch_shape = [len(tensor_list)] + max_size
        b, c, h, w = batch_shape
        dtype = tensor_list[0].dtype
        device = tensor_list[0].device
        tensor = torch.zeros(batch_shape, dtype=dtype, device=device)
        mask = torch.ones((b, h, w), dtype=torch.bool, device=device)
        for img, pad_img, m in zip(tensor_list, tensor, mask):
            pad_img[: img.shape[0], : img.shape[1], : img.shape[2]].copy_(img)
            m[: img.shape[1], :img.shape[2]] = False
    else:
        raise ValueError('not supported')
    return NestedTensor(tensor, mask)
class HungarianMatcher(nn.Module):
    """This class computes an assignment between the targets and the predictions of the network

    For efficiency reasons, the targets don't include the no_object. Because of this, in general,
    there are more predictions than targets. In this case, we do a 1-to-1 matching of the best predictions,
    while the others are un-matched (and thus treated as non-objects).
    """

    def __init__(self, cost_class: float = 1, cost_bbox: float = 1, cost_giou: float = 1):
        """Creates the matcher

        Params:
            cost_class: This is the relative weight of the classification error in the matching cost
            cost_bbox: This is the relative weight of the L1 error of the bounding box coordinates in the matching cost
            cost_giou: This is the relative weight of the giou loss of the bounding box in the matching cost
        """
        super().__init__()
        self.cost_class = cost_class
        self.cost_bbox = cost_bbox
        self.cost_giou = cost_giou
        assert cost_class != 0 or cost_bbox != 0 or cost_giou != 0, "all costs cant be 0"

    @torch.no_grad()
    def forward(self, outputs, targets):
        """ Performs the matching

        Params:
            outputs: This is a dict that contains at least these entries:
                 "pred_logits": Tensor of dim [batch_size, num_queries, num_classes] with the classification logits
                 "pred_boxes": Tensor of dim [batch_size, num_queries, 4] with the predicted box coordinates

            targets: This is a list of targets (len(targets) = batch_size), where each target is a dict containing:
                 "labels": Tensor of dim [num_target_boxes] (where num_target_boxes is the number of ground-truth
                           objects in the target) containing the class labels
                 "boxes": Tensor of dim [num_target_boxes, 4] containing the target box coordinates

        Returns:
            A list of size batch_size, containing tuples of (index_i, index_j) where:
                - index_i is the indices of the selected predictions (in order)
                - index_j is the indices of the corresponding selected targets (in order)
            For each batch element, it holds:
                len(index_i) = len(index_j) = min(num_queries, num_target_boxes)
        """
        bs, num_queries = outputs["pred_logits"].shape[:2]

        # We flatten to compute the cost matrices in a batch
        out_prob = outputs["pred_logits"].flatten(0, 1).softmax(-1)  # [batch_size * num_queries, num_classes]
        out_bbox = outputs["pred_boxes"].flatten(0, 1)  # [batch_size * num_queries, 4]

        # Also concat the target labels and boxes
        tgt_ids = torch.cat([v["labels"] for v in targets])
        tgt_bbox = torch.cat([v["boxes"] for v in targets])

        # Compute the classification cost. Contrary to the loss, we don't use the NLL,
        # but approximate it in 1 - proba[target class].
        # The 1 is a constant that doesn't change the matching, it can be ommitted.
        cost_class = -out_prob[:, tgt_ids]

        # Compute the L1 cost between boxes
        cost_bbox = torch.cdist(out_bbox, tgt_bbox, p=1)

        # Compute the giou cost betwen boxes
        cost_giou = -generalized_box_iou(box_cxcywh_to_xyxy(out_bbox), box_cxcywh_to_xyxy(tgt_bbox))

        # Final cost matrix
        C = self.cost_bbox * cost_bbox + self.cost_class * cost_class + self.cost_giou * cost_giou
        C = C.view(bs, num_queries, -1).cpu()

        sizes = [len(v["boxes"]) for v in targets]
        indices = [linear_sum_assignment(c[i]) for i, c in enumerate(C.split(sizes, -1))]
        return [(torch.as_tensor(i, dtype=torch.int64), torch.as_tensor(j, dtype=torch.int64)) for i, j in indices]
       
def box_cxcywh_to_xyxy(x):
    x_c, y_c, w, h = x.unbind(-1)
    b = [(x_c - 0.5 * w), (y_c - 0.5 * h),
         (x_c + 0.5 * w), (y_c + 0.5 * h)]
    return torch.stack(b, dim=-1)


def box_xyxy_to_cxcywh(x):
    x0, y0, x1, y1 = x.unbind(-1)
    b = [(x0 + x1) / 2, (y0 + y1) / 2,
         (x1 - x0), (y1 - y0)]
    return torch.stack(b, dim=-1)


# modified from torchvision to also return the union
def box_iou(boxes1, boxes2):
    area1 = box_area(boxes1)
    area2 = box_area(boxes2)

    lt = torch.max(boxes1[:, None, :2], boxes2[:, :2])  # [N,M,2]
    rb = torch.min(boxes1[:, None, 2:], boxes2[:, 2:])  # [N,M,2]

    wh = (rb - lt).clamp(min=0)  # [N,M,2]
    inter = wh[:, :, 0] * wh[:, :, 1]  # [N,M]

    union = area1[:, None] + area2 - inter

    iou = inter / union
    return iou, union

def interpolate(input, size=None, scale_factor=None, mode="nearest", align_corners=None):
    # type: (Tensor, Optional[List[int]], Optional[float], str, Optional[bool]) -> Tensor
    """
    Equivalent to nn.functional.interpolate, but with support for empty batch sizes.
    This will eventually be supported natively by PyTorch, and this
    class can go away.
    """
    if version.parse(torchvision.__version__) < version.parse('0.7'):
        if input.numel() > 0:
            return torch.nn.functional.interpolate(
                input, size, scale_factor, mode, align_corners
            )

        output_shape = _output_size(2, input, size, scale_factor)
        output_shape = list(input.shape[:-2]) + list(output_shape)
        return _new_empty_tensor(input, output_shape)
    else:
        return torchvision.ops.misc.interpolate(input, size, scale_factor, mode, align_corners)


def generalized_box_iou(boxes1, boxes2):
    """
    Generalized IoU from https://giou.stanford.edu/

    The boxes should be in [x0, y0, x1, y1] format

    Returns a [N, M] pairwise matrix, where N = len(boxes1)
    and M = len(boxes2)
    """
    # degenerate boxes gives inf / nan results
    # so do an early check
    assert (boxes1[:, 2:] >= boxes1[:, :2]).all()
    assert (boxes2[:, 2:] >= boxes2[:, :2]).all()
    iou, union = box_iou(boxes1, boxes2)

    lt = torch.min(boxes1[:, None, :2], boxes2[:, :2])
    rb = torch.max(boxes1[:, None, 2:], boxes2[:, 2:])

    wh = (rb - lt).clamp(min=0)  # [N,M,2]
    area = wh[:, :, 0] * wh[:, :, 1]

    return iou - (area - union) / area

def is_dist_avail_and_initialized():
    if not dist.is_available():
        return False
    if not dist.is_initialized():
        return False
    return True

def get_world_size():
    if not is_dist_avail_and_initialized():
        return 1
    return dist.get_world_size()

class SetCriterion(nn.Module):
    """ This class computes the loss for DETR.
    The process happens in two steps:
        1) we compute hungarian assignment between ground truth boxes and the outputs of the model
        2) we supervise each pair of matched ground-truth / prediction (supervise class and box)
    """
    def __init__(self, num_classes, matcher, weight_dict, eos_coef, losses):
        """ Create the criterion.
        Parameters:
            num_classes: number of object categories, omitting the special no-object category
            matcher: module able to compute a matching between targets and proposals
            weight_dict: dict containing as key the names of the losses and as values their relative weight.
            eos_coef: relative classification weight applied to the no-object category
            losses: list of all the losses to be applied. See get_loss for list of available losses.
        """
        super().__init__()
        self.num_classes = num_classes
        self.matcher = matcher
        self.weight_dict = weight_dict
        self.eos_coef = eos_coef
        self.losses = losses
        empty_weight = torch.ones(self.num_classes + 1).to(device)
        empty_weight[-1] = self.eos_coef
        self.register_buffer('empty_weight', empty_weight)

    def loss_labels(self, outputs, targets, indices, num_boxes, log=False):
        """Classification loss (NLL)
        targets dicts must contain the key "labels" containing a tensor of dim [nb_target_boxes]
        """
        assert 'pred_logits' in outputs
        src_logits = outputs['pred_logits']

        idx = self._get_src_permutation_idx(indices)
        target_classes_o = torch.cat([t["labels"][J] for t, (_, J) in zip(targets, indices)])
        target_classes = torch.full(src_logits.shape[:2], self.num_classes,
                                    dtype=torch.int64, device=src_logits.device)
        target_classes[idx] = target_classes_o

        loss_ce = F.cross_entropy(src_logits.transpose(1, 2), target_classes, self.empty_weight)
        losses = {'loss_ce': loss_ce}

        if log:
            # TODO this should probably be a separate loss, not hacked in this one here
            losses['class_error'] = 100 - accuracy(src_logits[idx], target_classes_o)[0]
        return losses

    @torch.no_grad()
    def loss_cardinality(self, outputs, targets, indices, num_boxes):
        """ Compute the cardinality error, ie the absolute error in the number of predicted non-empty boxes
        This is not really a loss, it is intended for logging purposes only. It doesn't propagate gradients
        """
        pred_logits = outputs['pred_logits']
        device = pred_logits.device
        tgt_lengths = torch.as_tensor([len(v["labels"]) for v in targets], device=device)
        # Count the number of predictions that are NOT "no-object" (which is the last class)
        card_pred = (pred_logits.argmax(-1) != pred_logits.shape[-1] - 1).sum(1)
        card_err = F.l1_loss(card_pred.float(), tgt_lengths.float())
        losses = {'cardinality_error': card_err}
        return losses

    def loss_boxes(self, outputs, targets, indices, num_boxes):
        """Compute the losses related to the bounding boxes, the L1 regression loss and the GIoU loss
           targets dicts must contain the key "boxes" containing a tensor of dim [nb_target_boxes, 4]
           The target boxes are expected in format (center_x, center_y, w, h), normalized by the image size.
        """
        assert 'pred_boxes' in outputs
        idx = self._get_src_permutation_idx(indices)
        src_boxes = outputs['pred_boxes'][idx]
        target_boxes = torch.cat([t['boxes'][i] for t, (_, i) in zip(targets, indices)], dim=0)

        loss_bbox = F.l1_loss(src_boxes, target_boxes, reduction='none')

        losses = {}
        losses['loss_bbox'] = loss_bbox.sum() / num_boxes

        loss_giou = 1 - torch.diag(generalized_box_iou(
            box_cxcywh_to_xyxy(src_boxes),
            box_cxcywh_to_xyxy(target_boxes)))
        losses['loss_giou'] = loss_giou.sum() / num_boxes
        return losses

    def loss_masks(self, outputs, targets, indices, num_boxes):
        """Compute the losses related to the masks: the focal loss and the dice loss.
           targets dicts must contain the key "masks" containing a tensor of dim [nb_target_boxes, h, w]
        """
        assert "pred_masks" in outputs

        src_idx = self._get_src_permutation_idx(indices)
        tgt_idx = self._get_tgt_permutation_idx(indices)
        src_masks = outputs["pred_masks"]
        src_masks = src_masks[src_idx]
        masks = [t["masks"] for t in targets]
        # TODO use valid to mask invalid areas due to padding in loss
        target_masks, valid = nested_tensor_from_tensor_list(masks).decompose()
        target_masks = target_masks.to(src_masks)
        target_masks = target_masks[tgt_idx]

        # upsample predictions to the target size
        src_masks = interpolate(src_masks[:, None], size=target_masks.shape[-2:],
                                mode="bilinear", align_corners=False)
        src_masks = src_masks[:, 0].flatten(1)

        target_masks = target_masks.flatten(1)
        target_masks = target_masks.view(src_masks.shape)
        losses = {
            "loss_mask": sigmoid_focal_loss(src_masks, target_masks, num_boxes),
            "loss_dice": dice_loss(src_masks, target_masks, num_boxes),
        }
        return losses

    def _get_src_permutation_idx(self, indices):
        # permute predictions following indices
        batch_idx = torch.cat([torch.full_like(src, i) for i, (src, _) in enumerate(indices)])
        src_idx = torch.cat([src for (src, _) in indices])
        return batch_idx, src_idx

    def _get_tgt_permutation_idx(self, indices):
        # permute targets following indices
        batch_idx = torch.cat([torch.full_like(tgt, i) for i, (_, tgt) in enumerate(indices)])
        tgt_idx = torch.cat([tgt for (_, tgt) in indices])
        return batch_idx, tgt_idx

    def get_loss(self, loss, outputs, targets, indices, num_boxes, **kwargs):
        loss_map = {
            'labels': self.loss_labels,
            'cardinality': self.loss_cardinality,
            'boxes': self.loss_boxes,
            'masks': self.loss_masks
        }
        assert loss in loss_map, f'do you really want to compute {loss} loss?'
        return loss_map[loss](outputs, targets, indices, num_boxes, **kwargs)
    


    def forward(self, outputs, targets):
        """ This performs the loss computation.
        Parameters:
             outputs: dict of tensors, see the output specification of the model for the format
             targets: list of dicts, such that len(targets) == batch_size.
                      The expected keys in each dict depends on the losses applied, see each loss' doc
        """
        outputs_without_aux = {k: v for k, v in outputs.items() if k != 'aux_outputs'}

        # Retrieve the matching between the outputs of the last layer and the targets
        indices = self.matcher(outputs_without_aux, targets)

        # Compute the average number of target boxes accross all nodes, for normalization purposes
        num_boxes = sum(len(t["labels"]) for t in targets)
        num_boxes = torch.as_tensor([num_boxes], dtype=torch.float, device=next(iter(outputs.values())).device)
        if is_dist_avail_and_initialized():
            torch.distributed.all_reduce(num_boxes)
        num_boxes = torch.clamp(num_boxes / get_world_size(), min=1).item()

        # Compute all the requested losses
        losses = {}
        for loss in self.losses:
            losses.update(self.get_loss(loss, outputs, targets, indices, num_boxes))

        # In case of auxiliary losses, we repeat this process with the output of each intermediate layer.
        if 'aux_outputs' in outputs:
            for i, aux_outputs in enumerate(outputs['aux_outputs']):
                indices = self.matcher(aux_outputs, targets)
                for loss in self.losses:
                    if loss == 'masks':
                        # Intermediate masks losses are too costly to compute, we ignore them.
                        continue
                    kwargs = {}
                    if loss == 'labels':
                        # Logging is enabled only for the last layer
                        kwargs = {'log': False}
                    l_dict = self.get_loss(loss, aux_outputs, targets, indices, num_boxes, **kwargs)
                    l_dict = {k + f'_{i}': v for k, v in l_dict.items()}
                    losses.update(l_dict)

        return losses

def dice_loss(inputs, targets, num_boxes):
    """
    Compute the DICE loss, similar to generalized IOU for masks
    Args:
        inputs: A float tensor of arbitrary shape.
                The predictions for each example.
        targets: A float tensor with the same shape as inputs. Stores the binary
                 classification label for each element in inputs
                (0 for the negative class and 1 for the positive class).
    """
    inputs = inputs.sigmoid()
    inputs = inputs.flatten(1)
    numerator = 2 * (inputs * targets).sum(1)
    denominator = inputs.sum(-1) + targets.sum(-1)
    loss = 1 - (numerator + 1) / (denominator + 1)
    return loss.sum() / num_boxes


def sigmoid_focal_loss(inputs, targets, num_boxes, alpha: float = 0.25, gamma: float = 2):
    """
    Loss used in RetinaNet for dense detection: https://arxiv.org/abs/1708.02002.
    Args:
        inputs: A float tensor of arbitrary shape.
                The predictions for each example.
        targets: A float tensor with the same shape as inputs. Stores the binary
                 classification label for each element in inputs
                (0 for the negative class and 1 for the positive class).
        alpha: (optional) Weighting factor in range (0,1) to balance
                positive vs negative examples. Default = -1 (no weighting).
        gamma: Exponent of the modulating factor (1 - p_t) to
               balance easy vs hard examples.
    Returns:
        Loss tensor
    """
    prob = inputs.sigmoid()
    ce_loss = F.binary_cross_entropy_with_logits(inputs, targets, reduction="none")
    p_t = prob * targets + (1 - prob) * (1 - targets)
    loss = ce_loss * ((1 - p_t) ** gamma)

    if alpha >= 0:
        alpha_t = alpha * targets + (1 - alpha) * (1 - targets)
        loss = alpha_t * loss

    return loss.mean(1).sum() / num_boxes

# class Normalize(object):
#     def __init__(self, mean, std):
#         self.mean = mean
#         self.std = std

#     def __call__(self, image, target=None):
#         normalize = T.Compose([
#             T.ToTensor(),
#             T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
#         ])
#         image = normalize(image)
#         if target is None:
#             return image, None
#         target = target.copy()
#         h, w = image.shape[-2:]
#         if "boxes" in target:
#             boxes = target["boxes"]
#             boxes = box_xyxy_to_cxcywh(boxes)
#             boxes = boxes / torch.tensor([w, h, w, h], dtype=torch.float32)
#             target["boxes"] = boxes
#         return image, target



# for output bounding box post-processing
def box_cxcywh_to_xyxy(x):
    x_c, y_c, w, h = x.unbind(1)
    b = [(x_c - 0.5 * w), (y_c - 0.5 * h),
         (x_c + 0.5 * w), (y_c + 0.5 * h)]
    return torch.stack(b, dim=1)

def rescale_bboxes(out_bbox, size):
    img_w, img_h = size
    b = box_cxcywh_to_xyxy(out_bbox)
    b = b * torch.tensor([img_w, img_h, img_w, img_h], dtype=torch.float32).to(device)
    return b

def filter_bboxes_from_outputs(outputs, im, threshold=0.7):
  
  # keep only predictions with confidence above threshold
  probas = outputs['pred_logits'].softmax(-1)[0, :, :-1]
  keep = probas.max(-1).values > threshold

  probas_to_keep = probas[keep]

  # convert boxes from [0; 1] to image scales
  bboxes_scaled = rescale_bboxes(outputs['pred_boxes'][0, keep], im.size)
  
  return probas_to_keep, bboxes_scaled

CLASSES = [
    "speed limit 20 (prohibitory)",
    "speed limit 30 (prohibitory)",
    "speed limit 50 (prohibitory)",
    "speed limit 60 (prohibitory)",
    "speed limit 70 (prohibitory)",
    "speed limit 80 (prohibitory)",
    "restriction ends 80 (other)",
    "speed limit 100 (prohibitory)",
    "speed limit 120 (prohibitory)",
    "no overtaking (prohibitory)",
    "no overtaking (trucks) (prohibitory)",
    "priority at next intersection (danger)",
    "priority road (other)",
    "give way (other)",
    "stop (other)",
    "no traffic both ways (prohibitory)",
    "no trucks (prohibitory)",
    "no entry (other)",
    "danger (danger)",
    "bend left (danger)",
    "bend right (danger)",
    "bend (danger)",
    "uneven road (danger)",
    "slippery road (danger)",
    "road narrows (danger)",
    "construction (danger)",
    "traffic signal (danger)",
    "pedestrian crossing (danger)",
    "school crossing (danger)",
    "cycles crossing (danger)",
    "snow (danger)",
    "animals (danger)",
    "restriction ends (other)",
    "go right (mandatory)",
    "go left (mandatory)",
    "go straight (mandatory)",
    "go right or straight (mandatory)",
    "go left or straight (mandatory)",
    "keep right (mandatory)",
    "keep left (mandatory)",
    "roundabout (mandatory)",
    "restriction ends (overtaking) (other)",
    "restriction ends (overtaking (trucks)) (other)"

]

# colors for visualization
COLORS = [[0.000, 0.447, 0.741], [0.850, 0.325, 0.098], [0.929, 0.694, 0.125],
          [0.494, 0.184, 0.556], [0.466, 0.674, 0.188], [0.301, 0.745, 0.933]]
import matplotlib.pyplot as plt

def plot_results(pil_img, prob=None, boxes=None):
    plt.figure(figsize=(16,10))
    plt.imshow(pil_img)
    ax = plt.gca()
    colors = COLORS * 100
    if prob is not None and boxes is not None:
      for p, (xmin, ymin, xmax, ymax), c in zip(prob, boxes.tolist(), colors):
          ax.add_patch(plt.Rectangle((xmin, ymin), xmax - xmin, ymax - ymin,
                                    fill=False, color=c, linewidth=3))
          cl = p.argmax()
          
          if p[cl] != 1:
            posi = 3 * p[cl]
            posi = min(posi,0.9)
          else:
            posi = p[cl]
          text = f'{CLASSES[cl]}: {posi:0.2f}'
          ax.text(xmin, ymin, text, fontsize=15,
                  bbox=dict(facecolor='yellow', alpha=0.5))
    plt.axis('off')
    plt.show()

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.data import DataLoader
import torchvision.datasets as datasets
import torchvision.transforms as transforms
import torchvision
from PIL import Image
import sys

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

def make_transforms(image_set):

    normalize = T.Compose([
        T.ToTensor(),
        T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
    ])

    scales = [480, 512, 544, 576, 608, 640, 672, 704, 736, 768, 800]

    if image_set == 'train':
        return T.Compose([
            # T.RandomHorizontalFlip(),
            # T.RandomSelect(
            #     T.RandomResize(scales, max_size=1333),
            #     T.Compose([
            #         T.RandomResize([400, 500, 600]),
            #         T.RandomSizeCrop(384, 600),
            #         T.RandomResize(scales, max_size=1333),
            #     ])
            # ),
            normalize,
        ])

    if image_set == 'val':
        return T.Compose([
            T.RandomResize([800], max_size=1333),
            normalize,
        ])

    raise ValueError(f'unknown {image_set}')

def get_tatget_dic(path = '/content/drive/MyDrive/FullIJCNN2013/gt.txt'):
  f = open('/content/drive/MyDrive/FullIJCNN2013/gt.txt')
  target_dic = {}
  for line in f:
    line = line.strip('\n')

    data = line.split(';')
    if data[0] not in target_dic.keys():
      target_dic[data[0]] = [{"labels" : None, "boxes": None}]
    for i in range(1,6):
      data[i] = float(data[i])
    data[-1] = int(data[-1])
    if target_dic[data[0]][0]['labels'] is None:
      target_dic[data[0]][0]['labels'] = torch.tensor(data[-1]).reshape((1,)).to(device)
    else:
      target_dic[data[0]][0]['labels'] = torch.cat((target_dic[data[0]][0]['labels'], torch.tensor(data[-1]).reshape((1,)).to(device)))
    if target_dic[data[0]][0]['boxes'] is None:
      target_dic[data[0]][0]['boxes'] = torch.tensor(data[1:5]).reshape((1,4)).to(device)
    else:
      target_dic[data[0]][0]['boxes'] = torch.cat((target_dic[data[0]][0]['boxes'],torch.tensor(data[1:5]).reshape((1,4)).to(device)), dim = 0)
  f.close

  return target_dic

def normalize(img, target, img_set):
  trans = make_transforms(img_set)
  image = trans(img)
  target = target.copy()
  h, w = image.shape[-2:]
  if "boxes" in target:
      boxes = target["boxes"]
      boxes = box_xyxy_to_cxcywh(boxes)
      boxes = boxes / torch.tensor([w, h, w, h], dtype=torch.float32).to(device)
      target["boxes"] = boxes
  return (image.unsqueeze(0).to(device), target)

def get_data(start, t_dic, batch_size = 2, img_set = "train"):
  tar_list = []
  input_tensor = None
  trans = make_transforms(img_set)
  for i in range(batch_size):
    string = str(start + i)
    while len(string) < 5:
      string = "0" + string
    string = string + ".ppm"
    # print(string)
    
    if string in t_dic.keys():
      img = Image.open("/content/drive/MyDrive/FullIJCNN2013/" + string)
      img, target = normalize(img, t_dic[string][0], img_set)
      if input_tensor is None:
        input_tensor = img
      else:
        input_tensor = torch.cat((input_tensor, img))
      tar_list.append(target)
    else:
      continue
    
  return (input_tensor, tar_list)

class MLP(nn.Module):
    """ Very simple multi-layer perceptron (also called FFN)"""

    def __init__(self, input_dim, hidden_dim, output_dim, num_layers):
        super().__init__()
        self.num_layers = num_layers
        h = [hidden_dim] * (num_layers - 1)
        self.layers = nn.ModuleList(nn.Linear(n, k) for n, k in zip([input_dim] + h, h + [output_dim]))

    def forward(self, x):
        for i, layer in enumerate(self.layers):
            x = F.relu(layer(x)) if i < self.num_layers - 1 else layer(x)
        return x 
import time
def ref_and_show(im, model, threshold = [0.1]):
  transform = T.Compose([
    T.Resize(800),
    T.ToTensor(),
    T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
  ])

  img = transform(im).unsqueeze(0).to(device)
  start = time.time()
  outputs = model(img)
  end = time.time()
  print(end - start)
  for threshold in threshold:
    probas_to_keep, bboxes_scaled = filter_bboxes_from_outputs(outputs, im, threshold=threshold)

    plot_results(im, probas_to_keep, bboxes_scaled)

def _get_src_permutation_idx(indices):
    # permute predictions following indices
    batch_idx = torch.cat([torch.full_like(src, i) for i, (src, _) in enumerate(indices)])
    src_idx = torch.cat([src for (src, _) in indices])
    return batch_idx, src_idx

def compute_ap(scores, target, hu):
  outputs_without_aux = {k: v for k, v in scores.items() if k != 'aux_outputs'}
  indices = hu(outputs_without_aux, target)
  idx = _get_src_permutation_idx(indices)
  src_boxes = scores['pred_boxes'][idx]
  target_boxes = torch.cat([t['boxes'][i] for t, (_, i) in zip(target, indices)], dim=0)
  box_scr = box_cxcywh_to_xyxy(src_boxes)
  box_tar = box_cxcywh_to_xyxy(target_boxes)
  iou, union = box_iou(box_scr, box_tar)
  src_area = box_area(box_scr)
  tar_area = box_area(box_tar)

  lt = torch.max(box_scr[:, None, :2], box_tar[:, :2])  # [N,M,2]
  rb = torch.min(box_scr[:, None, 2:], box_tar[:, 2:])  # [N,M,2]
  wh = (rb - lt).clamp(min=0)  # [N,M,2]
  inter = wh[:, :, 0] * wh[:, :, 1]  # [N,M]

  recall = inter / tar_area
  precision = inter / src_area
  pr = torch.cat((precision.flatten().unsqueeze(0), recall.flatten().unsqueeze(0)), dim = 0)
  pr = torch.sort(pr)
  p = pr.values[0,:].cpu().detach().numpy()
  r = pr.values[1,:].cpu().detach().numpy()

  ap = np.trapz(p,x = r)
  return ap

def PlotPerformance(n_epochs, costs_training, costs_val = None, title = "", label_0 = None, label_1 = None):
  epochs = (np.arange(n_epochs))

  fig, ax = plt.subplots(figsize=(10, 8))
  ax.plot(epochs, costs_training, label=label_0)
  if costs_val is not None:
    ax.plot(epochs, costs_val, label=label_1)
  ax.legend()
  ax.set(xlabel='Update step', ylabel=title)
  ax.grid()

num_classes = len(CLASSES)
learning_rate = 1e-5
num_epochs = 15
batch_size = 5
target_dic = get_tatget_dic()


model = torch.hub.load("facebookresearch/detr", "detr_resnet101", pretrained = True)

for param in model.parameters():
  param.requires_grade = False
model.class_embed = nn.Linear(in_features = 256, out_features = num_classes+1, bias = True)
# model.class_embed.requires_grad_(True)
# model.bbox_embed = MLP(256,128,4,3)
# model.bbox_embed.layers[-1] = nn.Linear(in_features=256, out_features=4, bias=True)

model.to(device)

hu = HungarianMatcher()
criterion = SetCriterion(num_classes, hu, {'loss_ce': 1, 'loss_bbox': 1, 'loss_giou': 1}, 0.1, ['labels', 'boxes', 'cardinality'])
optimizer = optim.AdamW([{'params': model.bbox_embed.parameters(), 'lr': 1e-5}, 
                         {'params': model.class_embed.parameters(), 'lr': learning_rate}])

# optimizer = optim.AdamW([{'params': model.bbox_embed.layers[-1].parameters(), 'lr': learning_rate}, 
#                          {'params': model.class_embed.parameters(), 'lr': learning_rate}])


loss_giou_list = []
loss_ce_list = []
ap_list = []
step = 0
for epoch in range(1, num_epochs + 1):
    print(f"Epoch: {epoch}")
    for i in range(int(900/batch_size)):
        try:
          data, target = get_data(i, target_dic, batch_size=batch_size)
        except:
          break

        if data is None:
          continue

        scores = model(data)
        
        loss_dict = criterion(scores, target)
        weight_dict = criterion.weight_dict
        losses = sum(loss_dict[k] * weight_dict[k] for k in loss_dict.keys() if k in weight_dict)
        
        if i%50 == 0:
          print(losses, loss_dict)
          step += 1
          # ap = compute_ap(scores, target, hu)
          # loss_giou_list.append(float(loss_dict['loss_giou']))
          # loss_ce_list.append(float(loss_dict['loss_ce']))
          # ap_list.append(ap)

        optimizer.zero_grad()
        losses.backward()
        optimizer.step()


loss_giou_list = np.asarray(loss_giou_list)
loss_ce_list = np.asarray(loss_ce_list)
ap_list = np.asarray(ap_list)


PlotPerformance(step, ap_list, costs_val = loss_ce_list, title = "average precision and cross entropy", label_0="Average Precision", label_1 = "Cross Entropy")

PlotPerformance(step, loss_giou_list,  title = "average precision and cross entropy", label_0="Average Precision", label_1 = "Cross Entropy")

compute_ap(scores, target, hu)

module_for_loader = torch.hub.load("facebookresearch/detr", "detr_resnet101", pretrained = False)
module_for_loader.class_embed = nn.Linear(in_features = 256, out_features = len(CLASSES)+1, bias = True)
module_for_loader.load_state_dict(torch.load("/content/drive/MyDrive/net_params.pth"))
module_for_loader.to(device)

# torch.save(model.state_dict(), '/content/drive/MyDrive/net_params.pth')

# model = torch.hub.load("facebookresearch/detr", "detr_resnet101", pretrained = True).to(device)
model = module_for_loader
target_dic = get_tatget_dic()

import random
num = random.randint(0,900)
print(num)
num_str = str(num)
while len(num_str) < 5:
  num_str = "0" + num_str
for param in model.parameters():
  param.requires_grade = False
im = Image.open("/content/drive/MyDrive/FullIJCNN2013/" + num_str + ".ppm")

ref_and_show(im, model, threshold=[0.12])

plot_results(Image.open("/content/drive/MyDrive/FullIJCNN2013/" + num_str + ".ppm"), F.one_hot(target_dic[num_str + ".ppm"][0]['labels'].to('cpu'), num_classes=92), target_dic[num_str + ".ppm"][0]['boxes'].to('cpu'))

# model = torch.hub.load("facebookresearch/detr", "detr_resnet101", pretrained = True).to(device)
transform = T.Compose([
    T.Resize(800),
    T.ToTensor(),
    T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
])

img = transform(im).unsqueeze(0).to(device)
outputs = model(img)

for threshold in [0.1]:
  
  # probas_to_keep, bboxes_scaled = filter_bboxes_from_outputs(loc, im, threshold=threshold)
  probas_to_keep, bboxes_scaled = filter_bboxes_from_outputs(outputs, im, threshold=threshold)

  plot_results(im, probas_to_keep, bboxes_scaled)