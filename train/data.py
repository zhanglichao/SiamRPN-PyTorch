# -*- coding: utf-8 -*-
import os
import sys
import cv2
import time
import torch
import random
import numpy as np
import os.path as osp
from config import Config as config
from torch.utils.data import Dataset
from got10k.datasets import ImageNetVID, GOT10k
from torchvision import datasets, transforms, utils
from PIL import Image, ImageOps, ImageStat, ImageDraw


class Anchor_ms(object):

    def __init__(self, feature_w, feature_h):
        self.w      = feature_w
        self.h      = feature_h
        self.base   = 64                   # base size for anchor box
        self.stride = config.stride * 2 - 1# center point shift stride
        self.scale  = config.ratios        # aspect ratio
        self.anchors= self.gen_anchors()   # xywh
        self.eps    = config.eps

    def gen_single_anchor(self):
        scale = np.array(self.scale, dtype = np.float32)
        s = self.base * self.base
        w, h = np.sqrt(s/scale), np.sqrt(s*scale)
        c_x, c_y = (self.stride-1)//2, (self.stride-1)//2
        anchor = np.vstack([c_x*np.ones_like(scale, dtype=np.float32), c_y*np.ones_like(scale, dtype=np.float32), w, h]).transpose()
        anchor = self.center_to_corner(anchor)
        return anchor

    def gen_anchors(self):
        anchor=self.gen_single_anchor()
        k = anchor.shape[0]
        delta_x, delta_y = [x*self.stride for x in range(self.w)], [y*self.stride for y in range(self.h)]
        shift_x, shift_y = np.meshgrid(delta_x, delta_y)
        shifts = np.vstack([shift_x.ravel(), shift_y.ravel(), shift_x.ravel(), shift_y.ravel()]).transpose()
        a = shifts.shape[0]
        anchors = (anchor.reshape((1,k,4))+shifts.reshape((a,1,4))).reshape((a*k, 4)) # corner format
        anchors = self.corner_to_center(anchors)
        return anchors

    # float
    def diff_anchor_gt(self, gt):
        eps = self.eps
        anchors, gt = self.anchors.copy(), gt.copy()
        diff = np.zeros_like(anchors, dtype = np.float32)
        diff[:,0] = (gt[0] - anchors[:,0])/(anchors[:,2] + eps)
        diff[:,1] = (gt[1] - anchors[:,1])/(anchors[:,3] + eps)
        diff[:,2] = np.log((gt[2] + eps)/(anchors[:,2] + eps))
        diff[:,3] = np.log((gt[3] + eps)/(anchors[:,3] + eps))
        return diff

    # float
    def center_to_corner(self, box):
        box = box.copy()
        box_ = np.zeros_like(box, dtype = np.float32)
        box_[:,0]=box[:,0]-(box[:,2]-1)/2
        box_[:,1]=box[:,1]-(box[:,3]-1)/2
        box_[:,2]=box[:,0]+(box[:,2]-1)/2
        box_[:,3]=box[:,1]+(box[:,3]-1)/2
        box_ = box_.astype(np.float32)
        return box_

    # float
    def corner_to_center(self, box):
        box = box.copy()
        box_ = np.zeros_like(box, dtype = np.float32)
        box_[:,0]=box[:,0]+(box[:,2]-box[:,0])/2
        box_[:,1]=box[:,1]+(box[:,3]-box[:,1])/2
        box_[:,2]=(box[:,2]-box[:,0])
        box_[:,3]=(box[:,3]-box[:,1])
        box_ = box_.astype(np.float32)
        return box_

    def pos_neg_anchor(self, gt, pos_num=16, neg_num=48, threshold_pos=0.5, threshold_neg=0.1):
        gt = gt.copy()
        gt_corner = self.center_to_corner(np.array(gt, dtype = np.float32).reshape(1, 4))
        an_corner = self.center_to_corner(np.array(self.anchors, dtype = np.float32))
        iou_value = self.iou(an_corner, gt_corner).reshape(-1) #(1445)
        max_iou   = max(iou_value)
        pos, neg  = np.zeros_like(iou_value, dtype=np.int32), np.zeros_like(iou_value, dtype=np.int32)

        # pos
        pos_cand = np.argsort(iou_value)[::-1][:30]
        pos_index = np.random.choice(pos_cand, pos_num, replace = False)
        if max_iou > threshold_pos:
            pos[pos_index] = 1

        # neg
        neg_cand = np.where(iou_value < threshold_neg)[0]
        neg_ind = np.random.choice(neg_cand, neg_num, replace = False)
        neg[neg_ind] = 1

        return pos, neg

    def iou(self,box1,box2):
        box1, box2 = box1.copy(), box2.copy()
        N=box1.shape[0]
        K=box2.shape[0]
        box1=np.array(box1.reshape((N,1,4)))+np.zeros((1,K,4))#box1=[N,K,4]
        box2=np.array(box2.reshape((1,K,4)))+np.zeros((N,1,4))#box1=[N,K,4]
        x_max=np.max(np.stack((box1[:,:,0],box2[:,:,0]),axis=-1),axis=2)
        x_min=np.min(np.stack((box1[:,:,2],box2[:,:,2]),axis=-1),axis=2)
        y_max=np.max(np.stack((box1[:,:,1],box2[:,:,1]),axis=-1),axis=2)
        y_min=np.min(np.stack((box1[:,:,3],box2[:,:,3]),axis=-1),axis=2)
        tb=x_min-x_max
        lr=y_min-y_max
        tb[np.where(tb<0)]=0
        lr[np.where(lr<0)]=0
        over_square=tb*lr
        all_square=(box1[:,:,2]-box1[:,:,0])*(box1[:,:,3]-box1[:,:,1])+(box2[:,:,2]-box2[:,:,0])*(box2[:,:,3]-box2[:,:,1])-over_square
        return over_square/all_square

class TrainDataLoader(Dataset):
    def __init__(self, seq_dataset, name):

        self.anchor_generator = Anchor_ms(config.out_feature, config.out_feature)
        self.max_inter        = config.max_inter
        self.sub_class_dir    = seq_dataset
        self.anchors          = self.anchor_generator.gen_anchors() #centor
        self.ret              = {}
        self.count            = 0
        self.name             = name

    def get_transform_for_train(self):
        transform_list = []
        transform_list.append(transforms.ToTensor())
        transform_list.append(transforms.Normalize(mean=(0.5,0.5,0.5), std=(0.5,0.5,0.5)))
        return transforms.Compose(transform_list)

    # tuple
    def _average(self):
        assert self.ret.__contains__('template_img_path'), 'no template_img_path'
        assert self.ret.__contains__('detection_img_path'),'no detection_img_path'
        template = Image.open(self.ret['template_img_path'])
        detection= Image.open(self.ret['detection_img_path'])

        mean_template = tuple(map(round, ImageStat.Stat(template).mean))
        mean_detection= tuple(map(round, ImageStat.Stat(detection).mean))
        self.ret['mean_template'] = mean_template
        self.ret['mean_detection']= mean_detection

    def _pick_img_pairs(self, index_of_subclass):

        assert index_of_subclass < len(self.sub_class_dir), 'index_of_subclass should less than total classes'

        sub_class_img_name = self.sub_class_dir[index_of_subclass][0]
        sub_class_img_num  = len(sub_class_img_name)
        sub_class_gt_name  = 'groundtruth.txt'

        status = True
        while status:
            '''if self.max_inter >= sub_class_img_num-1:
                self.max_inter = sub_class_img_num//2'''

            template_index = np.clip(random.choice(range(0, max(1, sub_class_img_num - self.max_inter))), 0, sub_class_img_num-1)

            detection_index= np.clip(random.choice(range(1, max(2, self.max_inter))) + template_index, 0, sub_class_img_num-1)

            template_img_path, detection_img_path  = sub_class_img_name[template_index], sub_class_img_name[detection_index]

            cords_of_template_abs  = self.sub_class_dir[index_of_subclass][1][template_index]

            cords_of_detection_abs = self.sub_class_dir[index_of_subclass][1][detection_index]

            if cords_of_template_abs[2]*cords_of_template_abs[3]*cords_of_detection_abs[2]*cords_of_detection_abs[3] != 0:
                status = False
            else:
                print('Warning : Encounter object missing, reinitializing ...')

        # load infomation of template and detection
        self.ret['template_img_path']      = template_img_path
        self.ret['detection_img_path']     = detection_img_path
        self.ret['template_target_x1y1wh'] = cords_of_template_abs
        self.ret['detection_target_x1y1wh']= cords_of_detection_abs
        t1, t2 = self.ret['template_target_x1y1wh'].copy(), self.ret['detection_target_x1y1wh'].copy()
        self.ret['template_target_xywh']   = np.array([t1[0]+t1[2]//2, t1[1]+t1[3]//2, t1[2], t1[3]], np.float32)
        self.ret['detection_target_xywh']  = np.array([t2[0]+t2[2]//2, t2[1]+t2[3]//2, t2[2], t2[3]], np.float32)
        self.ret['anchors'] = self.anchors
        self._average()

    def _pad_crop_resize(self):
        template_img, detection_img = Image.open(self.ret['template_img_path']), Image.open(self.ret['detection_img_path'])

        w, h = template_img.size
        cx, cy, tw, th = self.ret['template_target_xywh']
        p = round((tw + th)/2, 2)
        template_square_size  = int(np.sqrt((tw + p)*(th + p))) #a
        detection_square_size = int(template_square_size * 2)   #A =2a

        # pad
        detection_lt_x, detection_lt_y = cx - detection_square_size//2, cy - detection_square_size//2
        detection_rb_x, detection_rb_y = cx + detection_square_size//2, cy + detection_square_size//2
        left   = -detection_lt_x if detection_lt_x < 0 else 0
        top    = -detection_lt_y if detection_lt_y < 0 else 0
        right  =  detection_rb_x - w if detection_rb_x > w else 0
        bottom =  detection_rb_y - h if detection_rb_y > h else 0
        padding = tuple(map(int, [left, top, right, bottom]))
        new_w, new_h = left + right + w, top + bottom + h

        # pad load
        self.ret['padding'] = padding
        self.ret['new_template_img_padding_size'] = (new_w, new_h)
        self.ret['new_template_img_padding']      = ImageOps.expand(template_img,  border=padding, fill=self.ret['mean_template'])
        self.ret['new_detection_img_padding']     = ImageOps.expand(detection_img, border=padding, fill=self.ret['mean_detection'])

        # crop
        tl = cx + left - template_square_size//2
        tt = cy + top  - template_square_size//2
        tr = new_w - tl - template_square_size
        tb = new_h - tt - template_square_size
        self.ret['template_cropped'] = ImageOps.crop(self.ret['new_template_img_padding'].copy(), (tl, tt, tr, tb))

        dl = np.clip(cx + left - detection_square_size//2, 0, new_w - detection_square_size)
        dt = np.clip(cy + top  - detection_square_size//2, 0, new_h - detection_square_size)
        dr = np.clip(new_w - dl - detection_square_size, 0, new_w - detection_square_size)
        db = np.clip(new_h - dt - detection_square_size, 0, new_h - detection_square_size )
        self.ret['detection_cropped']= ImageOps.crop(self.ret['new_detection_img_padding'].copy(), (dl, dt, dr, db))

        self.ret['detection_tlcords_of_original_image'] = (cx - detection_square_size//2 , cy - detection_square_size//2)
        self.ret['detection_tlcords_of_padding_image']  = (cx - detection_square_size//2 + left, cy - detection_square_size//2 + top)
        self.ret['detection_rbcords_of_padding_image']  = (cx + detection_square_size//2 + left, cy + detection_square_size//2 + top)

        # resize
        self.ret['template_cropped_resized'] = self.ret['template_cropped'].copy().resize((config.template_img_size, config.template_img_size))   #(127, 127)
        self.ret['detection_cropped_resized']= self.ret['detection_cropped'].copy().resize((config.detection_img_size, config.detection_img_size))#(271, 271)
        self.ret['template_cropprd_resized_ratio'] = round(config.template_img_size/template_square_size, 2)
        self.ret['detection_cropped_resized_ratio'] = round(config.detection_img_size/detection_square_size, 2)

        # compute target in detection, and then we will compute IOU
        # whether target in detection part
        x, y, w, h = self.ret['detection_target_xywh']
        self.ret['target_tlcords_of_padding_image'] = np.array([int(x+left-w//2), int(y+top-h//2)], dtype = np.float32)
        self.ret['target_rbcords_of_padding_image'] = np.array([int(x+left+w//2), int(y+top+h//2)], dtype = np.float32)


        ### use cords of padding to compute cords about detection
        ### modify cords because not all the object in the detection
        x11, y11 = self.ret['detection_tlcords_of_padding_image']
        x12, y12 = self.ret['detection_rbcords_of_padding_image']
        x21, y21 = self.ret['target_tlcords_of_padding_image']
        x22, y22 = self.ret['target_rbcords_of_padding_image']
        x1_of_d, y1_of_d, x3_of_d, y3_of_d = int(x21-x11), int(y21-y11), int(x22-x11), int(y22-y11)
        x1 = np.clip(x1_of_d, 0, x12-x11).astype(np.float32)
        y1 = np.clip(y1_of_d, 0, y12-y11).astype(np.float32)
        x2 = np.clip(x3_of_d, 0, x12-x11).astype(np.float32)
        y2 = np.clip(y3_of_d, 0, y12-y11).astype(np.float32)
        self.ret['target_in_detection_x1y1x2y2']=np.array([x1, y1, x2, y2], dtype = np.float32)


        cords_in_cropped_detection = np.array((x1, y1, x2, y2), dtype = np.float32)
        cords_in_cropped_resized_detection = (cords_in_cropped_detection * self.ret['detection_cropped_resized_ratio']).astype(np.int32)
        x1, y1, x2, y2 = cords_in_cropped_resized_detection
        cx, cy, w, h = (x1+x2)//2, (y1+y2)//2, x2-x1, y2-y1
        self.ret['target_in_resized_detection_x1y1x2y2'] = np.array((x1, y1, x2, y2), dtype = np.int32)
        self.ret['target_in_resized_detection_xywh']     = np.array((cx, cy, w,  h) , dtype = np.int32)
        self.ret['area_target_in_resized_detection'] = w * h

    def _generate_pos_neg_diff(self):
        gt_box_in_detection = self.ret['target_in_resized_detection_xywh'].copy()
        pos, neg = self.anchor_generator.pos_neg_anchor(gt_box_in_detection)
        diff     = self.anchor_generator.diff_anchor_gt(gt_box_in_detection)
        pos, neg, diff = pos.reshape((-1, 1)), neg.reshape((-1,1)), diff.reshape((-1, 4))
        class_target = np.array([-100.] * self.anchors.shape[0], np.int32)

        # pos
        pos_index = np.where(pos == 1)[0]
        pos_num = len(pos_index)
        self.ret['pos_anchors'] = np.array(self.ret['anchors'][pos_index, :], dtype=np.int32) if not pos_num == 0 else None
        if pos_num > 0:
            class_target[pos_index] = 1

        # neg
        neg_index = np.where(neg == 1)[0]
        neg_num = len(neg_index)
        class_target[neg_index] = 0

        # draw pos and neg anchor box

        class_logits = class_target.reshape(-1, 1)
        pos_neg_diff = np.hstack((class_logits, diff))
        self.ret['pos_neg_diff'] = pos_neg_diff
        return pos_neg_diff

    def _tranform(self):
        """PIL to Tensor"""
        template_pil = self.ret['template_cropped_resized'].copy()
        detection_pil= self.ret['detection_cropped_resized'].copy()
        pos_neg_diff = self.ret['pos_neg_diff'].copy()

        transform       = self.get_transform_for_train()
        template_tensor = transform(template_pil)
        detection_tensor= transform(detection_pil)
        self.ret['template_tensor']     = template_tensor#.unsqueeze(0)

        self.ret['detection_tensor']    = detection_tensor#.unsqueeze(0)

        self.ret['pos_neg_diff_tensor'] = torch.Tensor(pos_neg_diff)

    def __getitem__(self, index):

        index = random.choice(range(len(self.sub_class_dir)))
        if self.name == 'GOT-10k':
            if index == 8627 or index == 8629 or index == 9057 or index == 9058:
                index += 1

        self._pick_img_pairs(index)
        self._pad_crop_resize()
        self._generate_pos_neg_diff()
        self._tranform()
        self.count += 1
        return self.ret['template_tensor'], self.ret['detection_tensor'], self.ret['pos_neg_diff_tensor']

    def __len__(self):
        return config.train_epoch_size
