# ------------------------------------------------------------------------
# Deformable DETR
# Copyright (c) 2020 SenseTime. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 [see LICENSE for details]
# ------------------------------------------------------------------------
# Modified from DETR (https://github.com/facebookresearch/detr)
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
# ------------------------------------------------------------------------

"""
Train and eval functions used in main.py
"""
import math
import os
import sys
from typing import Iterable
import random
import numpy as np
import bbox_visualizer as bbv
import cv2
import argparse
from PIL import Image, ImageDraw
from torchvision import transforms
import matplotlib

import torch
import util.misc as utils
from datasets.coco_eval import CocoEvaluator
from datasets.voc_eval import VocEvaluator
from datasets.panoptic_eval import PanopticEvaluator
from datasets.data_prefetcher import data_prefetcher
from datasets.selfdet import selective_search
from util.box_ops import box_xyxy_to_cxcywh, box_cxcywh_to_xyxy
from util.plot_utils import plot_prediction
from matplotlib import pyplot as plt

def train_one_epoch(model: torch.nn.Module, swav_model: torch.nn.Module, criterion: torch.nn.Module,
                    data_loader: Iterable, optimizer: torch.optim.Optimizer,
                    device: torch.device, epoch: int, max_norm: float = 0):
    model.train()
    criterion.train()
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    metric_logger.add_meter('class_error', utils.SmoothedValue(window_size=1, fmt='{value:.2f}'))
    metric_logger.add_meter('grad_norm', utils.SmoothedValue(window_size=1, fmt='{value:.2f}'))
    header = 'Epoch: [{}]'.format(epoch)
    print_freq = 10

    prefetcher = data_prefetcher(data_loader, device, prefetch=True)
    samples, targets = prefetcher.next()

    # for samples, targets in metric_logger.log_every(data_loader, print_freq, header):
    for _ in metric_logger.log_every(range(len(data_loader)), print_freq, header):
        outputs = model(samples)
        if swav_model is not None:
            with torch.no_grad():
                for elem in targets:
                    elem['patches'] = swav_model(elem['patches'])
        loss_dict = criterion(outputs, targets)
        weight_dict = criterion.weight_dict
        losses = sum(loss_dict[k] * weight_dict[k] for k in loss_dict.keys() if k in weight_dict)

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = utils.reduce_dict(loss_dict)
        loss_dict_reduced_unscaled = {f'{k}_unscaled': v
                                      for k, v in loss_dict_reduced.items()}
        loss_dict_reduced_scaled = {k: v * weight_dict[k]
                                    for k, v in loss_dict_reduced.items() if k in weight_dict}
        losses_reduced_scaled = sum(loss_dict_reduced_scaled.values())

        loss_value = losses_reduced_scaled.item()

        if not math.isfinite(loss_value):
            print("Loss is {}, stopping training".format(loss_value))
            print(loss_dict_reduced)
            sys.exit(1)

        optimizer.zero_grad()
        losses.backward()
        if max_norm > 0:
            grad_total_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm)
        else:
            grad_total_norm = utils.get_total_grad_norm(model.parameters(), max_norm)
        optimizer.step()

        metric_logger.update(loss=loss_value, **loss_dict_reduced_scaled, **loss_dict_reduced_unscaled)
        metric_logger.update(class_error=loss_dict_reduced['class_error'])
        metric_logger.update(lr=optimizer.param_groups[0]["lr"])
        metric_logger.update(grad_norm=grad_total_norm)

        samples, targets = prefetcher.next()
    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}


@torch.no_grad()
def evaluate(model, criterion, postprocessors, data_loader, base_ds, device, output_dir):
    class_name = {'0': 'N/A', '1':'aeroplane','2':'bicycle','3':'bird','4':'boat',
                  '5':'bottle','6':'bus','7':'car', '8':'cat',
                  '9':'chair','10':'cow', '11':'diningtable','12':'dog',
                  '13':'horse','14':'motorbike','15':'person','16':'pottedplant',
                  '17':'sheep', '18':'sofa', '19':'train', '20':'tvmonitor'}
    output_dir = 'vis'
    model.eval()
    criterion.eval()

    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('class_error', utils.SmoothedValue(window_size=1, fmt='{value:.2f}'))
    header = 'Test:'

    iou_types = tuple(k for k in ('segm', 'bbox') if k in postprocessors.keys())
    coco_evaluator = (CocoEvaluator if 'COCO' in type(base_ds).__name__ else VocEvaluator)(base_ds, iou_types)
    # coco_evaluator.coco_eval[iou_types[0]].params.iouThrs = [0, 0.1, 0.5, 0.75]

    panoptic_evaluator = None
    if 'panoptic' in postprocessors.keys():
        panoptic_evaluator = PanopticEvaluator(
            data_loader.dataset.ann_file,
            data_loader.dataset.ann_folder,
            output_dir=os.path.join(output_dir, "panoptic_eval"),
        )

    for samples, targets in metric_logger.log_every(data_loader, 10, header):
        samples = samples.to(device)
        targets = [{k: v.to(device) for k, v in t.items()} for t in targets]

        image_filenames = [
            'img_2021000247.jpg',
            'img_2021000706.jpg',
            'img_2021001070.jpg',
            'img_2021003055.jpg',
            'img_2021007554.jpg',
            'img_2021008746.jpg'
        ]

        name = 'img_' + str(targets[0]["image_id"][0].item()) + '.jpg'

        if 'loaddet' in output_dir:
            outputs = dict(pred_logits = [], pred_boxes = [])
            for i, target in enumerate(targets):
                image_id = target['image_id'].item()
                loaded = torch.load(str(image_id) + '.pt', map_location = device)
                outputs['pred_logits'].append(loaded['pred_logits'])
                outputs['pred_boxes'].append(loaded['pred_boxes'])
            model = lambda *ignored: outputs
        
        outputs = model(samples)
        # top_k = max(10, len(targets[0]['boxes']))
        top_k = 10

        indices = outputs['pred_logits'][0].softmax(-1)[..., 1].sort(descending=True)[1][:top_k]
        predictied_boxes = torch.stack([outputs['pred_boxes'][0][i] for i in indices]).unsqueeze(0)
        logits = torch.stack([outputs['pred_logits'][0][i] for i in indices]).unsqueeze(0)
        ax = plt.gca()

        # Pred results
        # -------------------------
        image = samples.tensors[0:1][0]
        boxes = predictied_boxes
        logits = logits.cpu()

        # -------------------------
        # Measure AP 
        probas = logits.softmax(-1)[0, :, :-1]
        values, indices = probas.max(-1)
        keep = probas.max(-1).values > 0.01
        values = values[keep]
        indices = indices[keep]
        gts = targets[0]['boxes'].unsqueeze(0)
        gt_cls = targets[0]['labels']

        from sklearn.metrics import average_precision_score
        for ind in range(len(gt_cls.unique())):

            cls_num = gt_cls[ind].cpu()
            cls_pred = values[indices == cls_num]
            cls_gt_box = gts[:, gt_cls == cls_num].view(-1,4)

            if len(cls_pred) != 0:
                cls_pred_np = cls_pred.cpu().numpy()
                cls_gt_box_np = cls_gt_box.cpu().numpy()

                # Sorting predictions by scores in descending order
                sort_indices = cls_pred_np.argsort()[::-1]
                cls_pred_np = cls_pred_np[sort_indices]


                # PASCAL VOC AP
                true_positives = (cls_pred_np > 0.5)

                # Calculate precision and recall
                precision = true_positives.cumsum() / (np.arange(len(cls_pred_np)) + 1)
                recall = true_positives.cumsum() / len(cls_gt_box_np)

                # Calculate Average Precision
                ap = average_precision_score(true_positives, precision)

                if name in image_filenames:
                      with open('./ap.txt', 'a') as file:
                          file.write(f"{name} {ap}\n")

                # MS COCO AP
                aps = []
                thresholds = np.linspace(0, 1, 101)  # Adjust the number of thresholds as needed
                for threshold in thresholds:
                    # Calculate true positives
                    true_positives = (cls_pred_np > threshold)

                    # Calculate precision and recall
                    precision = true_positives.cumsum() / (np.arange(len(cls_pred_np)) + 1)
                    recall = true_positives.cumsum() / len(cls_gt_box_np)

                    # Calculate Average Precision
                    ap = average_precision_score(true_positives, precision)
                    aps.append(ap)
                ap = sum(aps) / len(aps)
            else:
                ap = 0
            print(f'AP for class {cls_num.item()}: {ap}')


        # c = 'g'
        # plot_result(image, boxes, logits, c, ax, True, class_name, targets[0]['labels'])
        # -------------------------
        # GT Results
        # image = samples.tensors[0:1][0]
        # boxes = targets[0]['boxes'].unsqueeze(0)
        # logits = torch.zeros(1, targets[0]['boxes'].shape[0], 4).to(logits)
        # c = 'r'
        # plot_result(image, boxes, logits, c, ax, False, class_name, targets[0]['labels'])
        # -------------------------
        ax.set_aspect('equal')
        ax.set_axis_off()

        plt.savefig(os.path.join(output_dir, f'img_{int(targets[0]["image_id"][0])}.jpg'))
        plt.clf()

        loss_dict = criterion(outputs, targets)
        weight_dict = criterion.weight_dict

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = utils.reduce_dict(loss_dict)
        loss_dict_reduced_scaled = {k: v * weight_dict[k]
                                    for k, v in loss_dict_reduced.items() if k in weight_dict}
        loss_dict_reduced_unscaled = {f'{k}_unscaled': v
                                      for k, v in loss_dict_reduced.items()}
        metric_logger.update(loss=sum(loss_dict_reduced_scaled.values()),
                             **loss_dict_reduced_scaled,
                             **loss_dict_reduced_unscaled)
        metric_logger.update(class_error=loss_dict_reduced['class_error'])

        orig_target_sizes = torch.stack([t["orig_size"] for t in targets], dim=0)
        results = postprocessors['bbox'](outputs, orig_target_sizes)

        if 'savedet' in output_dir:
            os.makedirs(output_dir, exist_ok=True)
            for i, target in enumerate(targets):
                image_id = target['image_id'].item()
                pred_logits = outputs['pred_logits'][i]
                pred_boxes = outputs['pred_boxes'][i]
                img_h, img_w = target['orig_size']
                pred_boxes_ = box_cxcywh_to_xyxy(pred_boxes) * torch.stack([img_w, img_h, img_w, img_h], dim=-1)
                torch.save(dict(image_id=image_id, target=target, pred_logits=pred_logits, pred_boxes=pred_boxes,
                                pred_boxes_=pred_boxes_), os.path.join(output_dir, str(image_id) + '.pt'))

        if 'segm' in postprocessors.keys():
            target_sizes = torch.stack([t["size"] for t in targets], dim=0)
            results = postprocessors['segm'](results, outputs, orig_target_sizes, target_sizes)
        res = {target['image_id'].item(): output for target, output in zip(targets, results)}
        if coco_evaluator is not None:
            coco_evaluator.update(res)

        if panoptic_evaluator is not None:
            res_pano = postprocessors["panoptic"](outputs, target_sizes, orig_target_sizes)
            for i, target in enumerate(targets):
                image_id = target["image_id"].item()
                file_name = f"{image_id:012d}.png"
                res_pano[i]["image_id"] = image_id
                res_pano[i]["file_name"] = file_name

            panoptic_evaluator.update(res_pano)

    # gather the stats from all processes
    # metric_logger.synchronize_between_processes()
    # print("Averaged stats:", metric_logger)
    # if coco_evaluator is not None:
    #     coco_evaluator.synchronize_between_processes()
    # if panoptic_evaluator is not None:
    #     panoptic_evaluator.synchronize_between_processes()

    # # accumulate predictions from all images
    # if coco_evaluator is not None:
    #     coco_evaluator.accumulate()
    #     coco_evaluator.summarize()
    # panoptic_res = None
    # if panoptic_evaluator is not None:
    #     panoptic_res = panoptic_evaluator.summarize()
    # stats = {k: meter.global_avg for k, meter in metric_logger.meters.items()}
    # if coco_evaluator is not None:
    #     if 'bbox' in postprocessors.keys():
    #         stats['coco_eval_bbox'] = coco_evaluator.coco_eval['bbox'].stats.tolist()
    #     if 'segm' in postprocessors.keys():
    #         stats['coco_eval_masks'] = coco_evaluator.coco_eval['segm'].stats.tolist()
    # if panoptic_res is not None:
    #     stats['PQ_all'] = panoptic_res["All"]
    #     stats['PQ_th'] = panoptic_res["Things"]
    #     stats['PQ_st'] = panoptic_res["Stuff"]
    return stats, coco_evaluator


def plot_results(pil_img, prob, boxes, ax, c, plot_prob=True, norm=True):
    from matplotlib import pyplot as plt
    image = plot_image(ax, pil_img, norm)
    if prob is not None and boxes is not None:
        for p, (xmin, ymin, xmax, ymax) in zip(prob, boxes.tolist()):
            ax.add_patch(plt.Rectangle((xmin, ymin), xmax - xmin, ymax - ymin,
                                       fill=False, color=c, linewidth=1))
            # if plot_prob:
            #     text = ''
            #     for prob in p:
            #         text += f'{prob.item():0.2f}_'
            #     ax.text(xmin, ymin, text, fontsize=15,
            #             bbox=dict(facecolor='yellow', alpha=0.5))

def plot_image(ax, img, norm):
    if norm:
        img = img * np.array([0.229, 0.224, 0.225]) + np.array([0.485, 0.456, 0.406])
        img = (img * 255)
    img = img.astype('uint8')
    ax.imshow(img)

def plot_result(image, boxes, logits, c, ax, prob_true, class_name, target_label):
    out_bbox = boxes[0]

    # remove zero-padding
    mask_rows = (image != 0).any(dim=0).any(dim=1)
    mask_cols = (image != 0).any(dim=0).any(dim=0)
    image_cropped = image[:, mask_rows, :]
    image = image_cropped[:, :, mask_cols]

    size = list(image.shape[1:])[::-1]
    img_w, img_h = size
    b = box_cxcywh_to_xyxy(out_bbox)
    bboxes_scaled0 = b * torch.tensor([img_w, img_h, img_w, img_h], dtype=torch.float32).to(out_bbox)
    # -------------------------
    probas = logits.softmax(-1)[0, :, :-1]
    keep = probas.max(-1).values > 0.01
    # -------------------------
    pil_img = image.permute(1,2,0).detach().cpu().numpy()
    prob, pred_cls = probas[keep].max(dim=-1)
    label_list = []
    if not prob_true:
        for i in range(len(target_label)):
            target_label[i] += 1
            target_label[i] = torch.clamp(target_label[i], max=20)
            label_list.append(class_name[str(target_label[i].item())])
    else:
        for i in range(len(pred_cls)):
            pred_cls[i] += 1
            pred_cls[i] = torch.clamp(pred_cls[i], max=20)
            label_list.append(class_name[str(pred_cls[i].item())])
    boxes = bboxes_scaled0[keep]

    image = plot_image(ax, pil_img, True)
    if prob is not None and boxes is not None:
        for p, (xmin, ymin, xmax, ymax), label in zip(prob, boxes.tolist(), label_list):
            ax.add_patch(plt.Rectangle((xmin, ymin), xmax - xmin, ymax - ymin,
                                       fill=False, color=c, linewidth=2))
            if prob_true:
                ax.text(xmin, ymin, label, fontsize=10, bbox=dict(facecolor='g', alpha=0.7))
            else:
                ax.text(xmin, ymax, label, fontsize=10, bbox=dict(facecolor='r', alpha=0.7))

@torch.no_grad()
def viz(model, criterion, postprocessors, data_loader, base_ds, device, output_dir):
    class_name = {'0': 'N/A', '1':'aeroplane','2':'bicycle','3':'bird','4':'boat',
                  '5':'bottle','6':'bus','7':'car', '8':'cat',
                  '9':'chair','10':'cow', '11':'diningtable','12':'dog',
                  '13':'horse','14':'motorbike','15':'person','16':'pottedplant',
                  '17':'sheep', '18':'sofa', '19':'train', '20':'tvmonitor'}
    output_dir = 'vis'
    model.eval()
    criterion.eval()

    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('class_error', utils.SmoothedValue(window_size=1, fmt='{value:.2f}'))

    for samples, targets in data_loader:
        samples = samples.to(device)
        targets = [{k: v.to(device) for k, v in t.items()} for t in targets]
        top_k = len(targets[0]['boxes'])

        outputs = model(samples)
        indices = outputs['pred_logits'][0].softmax(-1)[..., 1].sort(descending=True)[1][:top_k]
        predictied_boxes = torch.stack([outputs['pred_boxes'][0][i] for i in indices]).unsqueeze(0)
        logits = torch.stack([outputs['pred_logits'][0][i] for i in indices]).unsqueeze(0)
        ax = plt.gca()

        # Pred results
        # -------------------------
        image = samples.tensors[0:1][0]
        boxes = predictied_boxes
        logits = logits.cpu()
        c = 'g'
        plot_result(image, boxes, logits, c, ax, True, class_name, targets[0]['labels'])
        # -------------------------
        # GT Results
        image = samples.tensors[0:1][0]
        boxes = targets[0]['boxes'].unsqueeze(0)
        logits = torch.zeros(1, targets[0]['boxes'].shape[0], 4).to(logits)
        c = 'r'
        plot_result(image, boxes, logits, c, ax, False, class_name, targets[0]['labels'])
        # -------------------------
        ax.set_aspect('equal')
        ax.set_axis_off()

        plt.savefig(os.path.join(output_dir, f'img_{int(targets[0]["image_id"][0])}.jpg'))
        plt.clf()

# @torch.no_grad()
# def viz(model, criterion, postprocessors, data_loader, base_ds, device, output_dir):
#     import numpy as np
#     os.makedirs(output_dir, exist_ok=True)
#     model.eval()
#     criterion.eval()
# 
#     metric_logger = utils.MetricLogger(delimiter="  ")
#     metric_logger.add_meter('class_error', utils.SmoothedValue(window_size=1, fmt='{value:.2f}'))
# 
#     for samples, targets in data_loader:
#         samples = samples.to(device)
#         targets = [{k: v.to(device) for k, v in t.items()} for t in targets]
#         top_k = len(targets[0]['boxes'])
# 
#         outputs = model(samples)
#         indices = outputs['pred_logits'][0].softmax(-1)[..., 1].sort(descending=True)[1][:top_k]
#         predictied_boxes = torch.stack([outputs['pred_boxes'][0][i] for i in indices]).unsqueeze(0)
#         logits = torch.stack([outputs['pred_logits'][0][i] for i in indices]).unsqueeze(0)
#         fig, ax = plt.subplots(1, 3, figsize=(10,3), dpi=200)
# 
#         img = samples.tensors[0].cpu().permute(1,2,0).numpy()
#         img = img * np.array([0.229, 0.224, 0.225]) + np.array([0.485, 0.456, 0.406])
#         img = (img * 255)
#         img = img.astype('uint8')
#         h, w = img.shape[:-1]
# 
# 
#         # SS results
#         boxes_ss = get_ss_res(img, h, w, top_k)
#         plot_prediction(samples.tensors[0:1], boxes_ss, torch.zeros(1, boxes_ss.shape[1], 4).to(logits), ax[0], plot_prob=False)
#         ax[0].set_title('Selective Search')
# 
#         # Pred results
#         plot_prediction(samples.tensors[0:1], predictied_boxes, logits, ax[1], plot_prob=False)
#         ax[1].set_title('Prediction (Ours)')
# 
#         # GT Results
#         plot_prediction(samples.tensors[0:1], targets[0]['boxes'].unsqueeze(0), torch.zeros(1, targets[0]['boxes'].shape[0], 4).to(logits), ax[2], plot_prob=False)
#         ax[2].set_title('GT')
# 
#         for i in range(3):
#             ax[i].set_aspect('equal')
#             ax[i].set_axis_off()
# 
#         plt.savefig(os.path.join(output_dir, f'img_{int(targets[0]["image_id"][0])}.jpg'))

def get_ss_res(img, h, w, top_k):
    boxes = selective_search(img, h, w)[:top_k]
    boxes = torch.tensor(boxes).unsqueeze(0)
    boxes = box_xyxy_to_cxcywh(boxes)/torch.tensor([w, h, w, h])
    return boxes

