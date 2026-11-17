"""近似画面核验：局部特征小幅对齐 + RGB 结构相似度，不进行手部/姿态分类。

SSIM 公式来源：Wang et al., IEEE TIP 2004。
https://ece.uwaterloo.ca/~z70wang/research/ssim/
局部特征匹配与 RANSAC 参考 OpenCV 官方文档。
"""
from collections import OrderedDict
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

RECIPE='frame-structure-v1'


@dataclass
class Frame:
    rgb: object
    gray: object
    points: object
    descriptors: object


class FrameMatcher:
    """仅持有有界的预览特征缓存；不修改任何数据库或原图。"""
    def __init__(self,cache_size=32):
        self.cache_size=cache_size;self.cache=OrderedDict()
        cv2.setNumThreads(1)
        self.sift=cv2.SIFT_create(nfeatures=1200) if hasattr(cv2,'SIFT_create') else None

    def _frame(self,rgb):
        gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
        points,descriptors=self.sift.detectAndCompute(gray,None) if self.sift else ([],None)
        return Frame(rgb,gray,points,descriptors)

    def cached(self,key):
        if key in self.cache:
            self.cache.move_to_end(key);return self.cache[key]
        return None

    def prepare(self,image,key=None):
        if key is not None:
            cached=self.cached(key)
            if cached is not None:return cached
        resized=image.convert('RGB').copy();resized.thumbnail((480,480),Image.Resampling.LANCZOS)
        frame=self._frame(np.asarray(resized,dtype=np.uint8))
        if key is not None:
            self.cache[key]=frame
            while len(self.cache)>self.cache_size:self.cache.popitem(last=False)
        return frame

    @staticmethod
    def _ssim(first,second):
        first=first.astype(np.float32)/255;second=second.astype(np.float32)/255
        mean1=cv2.GaussianBlur(first,(11,11),1.5);mean2=cv2.GaussianBlur(second,(11,11),1.5)
        variance1=np.maximum(0,cv2.GaussianBlur(first*first,(11,11),1.5)-mean1*mean1)
        variance2=np.maximum(0,cv2.GaussianBlur(second*second,(11,11),1.5)-mean2*mean2)
        covariance=cv2.GaussianBlur(first*second,(11,11),1.5)-mean1*mean2
        value=((2*mean1*mean2+.01**2)*(2*covariance+.03**2))/((mean1*mean1+mean2*mean2+.01**2)*(variance1+variance2+.03**2))
        return np.clip(value,0,1).mean(axis=2)

    def compare(self,reference,candidate):
        height,width=reference.gray.shape
        candidate_height,candidate_width=candidate.gray.shape
        aspect=(candidate_width/candidate_height)/(width/height)
        if not .8<=aspect<=1.25:
            return {'score':0.,'comparable':False,'coverage':0.,'aligned':False,'reason':'画幅比例差异较大','recipe':RECIPE}
        if candidate.rgb.shape!=reference.rgb.shape:
            candidate=self._frame(cv2.resize(candidate.rgb,(width,height),interpolation=cv2.INTER_AREA))
        rgb=candidate.rgb;mask=np.ones((height,width),np.uint8);aligned=False;inliers=0
        if reference.descriptors is not None and candidate.descriptors is not None and len(reference.points)>=8 and len(candidate.points)>=8:
            pairs=cv2.BFMatcher().knnMatch(candidate.descriptors,reference.descriptors,k=2)
            good=[pair[0] for pair in pairs if len(pair)==2 and pair[0].distance<.72*pair[1].distance]
            if len(good)>=8:
                source=np.float32([candidate.points[m.queryIdx].pt for m in good])
                target=np.float32([reference.points[m.trainIdx].pt for m in good])
                transform,accepted=cv2.estimateAffinePartial2D(source,target,method=cv2.RANSAC,ransacReprojThreshold=3,maxIters=2000,confidence=.99)
                if transform is not None:
                    inliers=int(accepted.sum());scale=float(np.linalg.norm(transform[:,0]))
                    angle=float(np.degrees(np.arctan2(transform[1,0],transform[0,0])))
                    if inliers>=8 and inliers/len(good)>=.5 and .85<=scale<=1.18 and abs(angle)<=10 and np.max(np.abs(transform[:,2]))<=.15*max(width,height):
                        rgb=cv2.warpAffine(candidate.rgb,transform,(width,height),borderMode=cv2.BORDER_REFLECT)
                        mask=cv2.warpAffine(mask,transform,(width,height),flags=cv2.INTER_NEAREST,borderMode=cv2.BORDER_CONSTANT,borderValue=0)
                        aligned=True
        coverage=float(mask.mean())
        if coverage<.8:
            return {'score':0.,'comparable':False,'coverage':coverage,'aligned':aligned,'reason':'可比画面覆盖不足','recipe':RECIPE}
        # 去除变换和高斯窗口边缘，不让填充像素提高分数。
        valid=cv2.erode(mask,np.ones((11,11),np.uint8)).astype(bool)
        if not valid.any():valid=mask.astype(bool)
        similarity=self._ssim(reference.rgb,rgb)
        score=float(similarity[valid].mean())
        return {'score':min(1.,max(0.,score)),'comparable':True,'coverage':coverage,'aligned':aligned,'inliers':inliers,
                'changed_fraction':float((similarity[valid]<.65).mean()),'recipe':RECIPE}
