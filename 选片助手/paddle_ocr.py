"""PP-OCRv6 Small/Medium 的 Windows CPU 推理；与手机使用同一官方 ONNX 权重。

预处理、DB 四边形检测与 CTC 解码参考 PaddlePaddle/PaddleOCR
（Apache-2.0）；颜色顺序读取官方 inference.yml，高度 48。
"""
import io
import math
from pathlib import Path
import re
import time

PIPELINE_VERSION='cpu-ocr-rotation-v2'

def candidate_text(text):
    """只连接紧随款号的独立连字符后缀；不猜测 O/0、I/1 等字符。"""
    return re.sub(r'([A-Za-z0-9Ａ-Ｚａ-ｚ０-９]{4,40})\n([－—-][A-Za-z0-9Ａ-Ｚａ-ｚ０-９]{2,12})(?=\n|$)',r'\1\2',text)


def ordered_box(points):
    import numpy as np
    points=np.asarray(points,dtype=np.float32)
    # 按 x 分左右，再按 y 分上下，避免菱形的 sum/diff 极值重复选点。
    left,right=np.split(points[np.argsort(points[:,0],kind='stable')],2)
    left=left[np.argsort(left[:,1])];right=right[np.argsort(right[:,1])]
    return np.array([left[0],right[0],right[1],left[1]],dtype=np.float32)


class PaddleOcr:
    def __init__(self,item):
        start=time.perf_counter()
        import cv2
        import numpy as np
        import onnxruntime as ort
        import yaml
        self.cv2=cv2;self.np=np
        options=ort.SessionOptions();options.intra_op_num_threads=max(1,min(8,int(item.get('threads',4))))
        options.inter_op_num_threads=1
        # 空闲时不让推理线程自旋，避免后台常驻增加 CPU 占用。
        options.add_session_config_entry('session.intra_op.allow_spinning','0')
        self.det=ort.InferenceSession(str(item['detector']),options,providers=['CPUExecutionProvider'])
        self.rec=ort.InferenceSession(str(item['recognizer']),options,providers=['CPUExecutionProvider'])
        config=yaml.safe_load(Path(item['dictionary']).read_text(encoding='utf8'))
        decode=next((op['DecodeImage'] for op in config['PreProcess']['transform_ops'] if 'DecodeImage' in op),{})
        self.rec_img_mode=item.get('rec_img_mode',decode.get('img_mode','BGR'))
        self.rec_min_width=int(item.get('rec_min_width',320))
        self.characters=['']+[str(c) for c in config['PostProcess']['character_dict']]
        if self.characters[-1]!=' ':self.characters.append(' ')
        self.limit=int(item.get('det_limit',1280))
        self.batch_size=max(1,min(16,int(item.get('rec_batch_size',1))))
        self.load_ms=(time.perf_counter()-start)*1000

    def _boxes(self,prob,height,width):
        cv2,np=self.cv2,self.np
        ph,pw=prob.shape
        contours,_=cv2.findContours((prob>.3).astype('uint8')*255,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
        boxes=[]
        for contour in contours[:3000]:
            rect=cv2.minAreaRect(contour)
            if min(rect[1])<3:continue
            points=ordered_box(cv2.boxPoints(rect))
            xmin,ymin=np.floor(points.min(axis=0)).astype(int).clip(0,[pw-1,ph-1])
            xmax,ymax=np.ceil(points.max(axis=0)).astype(int).clip(0,[pw-1,ph-1])
            mask=np.zeros((ymax-ymin+1,xmax-xmin+1),dtype='uint8')
            cv2.fillPoly(mask,[(points-[xmin,ymin]).astype('int32')],1)
            if cv2.mean(prob[ymin:ymax+1,xmin:xmax+1],mask)[0]<.6:continue
            # 对 minAreaRect 四边形做圆角外扩，其最小包围矩形的两边各增加 2d。
            rw,rh=rect[1];distance=rw*rh*1.5/(2*(rw+rh))
            expanded=(rect[0],(rw+2*distance,rh+2*distance),rect[2])
            if min(expanded[1])<5:continue
            box=ordered_box(cv2.boxPoints(expanded))
            box=np.rint(box*np.array([width/pw,height/ph])).clip(0,[width,height]).astype('float32')
            if min(np.linalg.norm(box[1]-box[0]),np.linalg.norm(box[3]-box[0]))>3:boxes.append(box)
        boxes.sort(key=lambda b:(b[0,1],b[0,0]))
        # 同行从左到右；与官方读取顺序保持一致。
        for i in range(len(boxes)-1):
            for j in range(i,-1,-1):
                if abs(boxes[j+1][0,1]-boxes[j][0,1])<10 and boxes[j+1][0,0]<boxes[j][0,0]:
                    boxes[j],boxes[j+1]=boxes[j+1],boxes[j]
                else:break
        return boxes

    def _crop(self,image,box):
        cv2,np=self.cv2,self.np
        box=ordered_box(cv2.boxPoints(cv2.minAreaRect(box)))
        width=max(1,int(max(np.linalg.norm(box[0]-box[1]),np.linalg.norm(box[2]-box[3]))))
        height=max(1,int(max(np.linalg.norm(box[0]-box[3]),np.linalg.norm(box[1]-box[2]))))
        target=np.array([[0,0],[width,0],[width,height],[0,height]],dtype='float32')
        crop=cv2.warpPerspective(image,cv2.getPerspectiveTransform(box,target),(width,height),
                                 flags=cv2.INTER_CUBIC,borderMode=cv2.BORDER_REPLICATE)
        if height/width>=1.5:crop=np.rot90(crop)
        return crop

    def _detect(self,image):
        cv2,np=self.cv2,self.np
        height,width=image.shape[:2];ratio=min(1,self.limit/max(height,width))
        dh=max(32,round(int(height*ratio)/32)*32);dw=max(32,round(int(width*ratio)/32)*32)
        resized=cv2.resize(image,(dw,dh)).astype('float32')/255
        normalized=(resized-np.array([.485,.456,.406],dtype='float32'))/np.array([.229,.224,.225],dtype='float32')
        tensor=np.ascontiguousarray(normalized.transpose(2,0,1)[None])
        pred=self.det.run(None,{self.det.get_inputs()[0].name:tensor})[0][0,0]
        return self._boxes(pred,height,width),[1,3,dh,dw]

    def _read_crops(self,crops):
        cv2,np=self.cv2,self.np;results=[]
        for begin in range(0,len(crops),self.batch_size):
            batch=crops[begin:begin+self.batch_size]
            widths=[max(1,min(3200,math.ceil(48*c.shape[1]/c.shape[0]))) for c in batch]
            tensor=np.zeros((len(batch),3,48,max(self.rec_min_width,max(widths))),dtype='float32')
            for i,(crop,w) in enumerate(zip(batch,widths)):
                pixels=crop[:,:,::-1] if self.rec_img_mode=='RGB' else crop
                normalized=cv2.resize(pixels,(w,48)).astype('float32')/127.5-1
                tensor[i,:,:,:w]=normalized.transpose(2,0,1)
            outputs=self.rec.run(None,{self.rec.get_inputs()[0].name:tensor})[0]
            if outputs.shape[2]!=len(self.characters):raise ValueError('OCR 字典与识别模型不匹配')
            for output in outputs:
                indices=output.argmax(axis=1);scores=output.max(axis=1)
                keep=(indices!=0)&np.r_[True,indices[1:]!=indices[:-1]]
                text=''.join(self.characters[index] for index in indices[keep])
                results.append({'text':text,'confidence':float(scores[keep].mean()) if keep.any() else 0})
        return results

    def _page_direction(self,image,boxes):
        np=self.np
        # 长文本比单字/圆形 Logo 更可靠，避免衣服图案主导方向。
        selected=[]
        for box in boxes:
            ordered=ordered_box(self.cv2.boxPoints(self.cv2.minAreaRect(box)))
            w=float(np.linalg.norm(ordered[1]-ordered[0]));h=float(np.linalg.norm(ordered[3]-ordered[0]))
            if max(w,h)/max(1,min(w,h))<2.0:continue
            selected.append((box,ordered,w,h))
        selected=sorted(selected,key=lambda item:max(item[2],item[3]),reverse=True)[:6]
        if not selected:return 0,{'votes':[0,0,0,0],'confidence':0,'uncertain':True}
        crops=[self._crop(image,item[0]) for item in selected]
        readings=self._read_crops([c for crop in crops for c in (crop,np.ascontiguousarray(np.rot90(crop,2)))])
        votes=np.zeros(4,dtype='float64');count=0
        for index,((_,ordered,w,h),crop) in enumerate(zip(selected,crops)):
            pair=readings[index*2:index*2+2]
            # 短乱码可能有很高的单字分数，必须同时考虑有效字符数和双向差值。
            scores=[r['confidence']*min(1,sum(not c.isspace() for c in r['text'])/6) for r in pair]
            inverted=int(scores[1]>scores[0]);score=scores[inverted];margin=abs(scores[1]-scores[0])
            if score<.65 or margin<.04:continue
            vector=ordered[3]-ordered[0] if h/max(w,1)>=1.5 else ordered[1]-ordered[0]
            if inverted:vector=-vector
            quadrant=round(math.degrees(math.atan2(float(vector[1]),float(vector[0])))/90)%4
            aspect=crop.shape[1]/max(1,crop.shape[0]);votes[quadrant]+=min(12,aspect)*score*min(1,margin/.3);count+=1
        winner=int(votes.argmax());confidence=float(votes[winner]/votes.sum()) if votes.sum() else 0
        certain=confidence>=.7 and count>=1
        return (winner if certain else 0),{'votes':votes.tolist(),'confidence':confidence,'uncertain':not certain,'probe_lines':len(selected)}

    @staticmethod
    def source_points(points,quarter,width,height):
        import numpy as np
        points=np.asarray(points,dtype='float32');x=points[:,0];y=points[:,1]
        if quarter==1:points=np.column_stack((width-1-y,x))
        elif quarter==2:points=np.column_stack((width-1-x,height-1-y))
        elif quarter==3:points=np.column_stack((y,height-1-x))
        return points.clip(0,[width,height]).tolist()

    def recognize(self,image_bytes):
        from PIL import Image,ImageOps
        cv2,np=self.cv2,self.np;start=time.perf_counter()
        with Image.open(io.BytesIO(image_bytes)) as source:
            source=ImageOps.exif_transpose(source).convert('RGB')
            source.thumbnail((2400,2400),Image.Resampling.LANCZOS)
            image=np.asarray(source)[:,:,::-1].copy()
        height,width=image.shape[:2]
        boxes,shape=self._detect(image);first_detect_end=time.perf_counter()
        quarter,orientation=self._page_direction(image,boxes)
        direction_end=time.perf_counter()
        if quarter:
            image=np.ascontiguousarray(np.rot90(image,quarter))
            boxes,shape=self._detect(image)  # 转正后重新检测，修复侧向检测拆字/断框。
        orientation.update(correction_ccw_degrees=quarter*90,method='bidirectional_text_consensus',source_size=[width,height])
        det_end=time.perf_counter()
        crops=[self._crop(image,box) for box in boxes];lines=[]
        for box,reading in zip(boxes,self._read_crops(crops)):
            if reading['text']:lines.append({**reading,'points':self.source_points(box,quarter,width,height),'reading_points':box.tolist()})
        end=time.perf_counter()
        return {'text':'\n'.join(line['text'] for line in lines),'lines':lines,
                'timing':{'load_ms':self.load_ms,'detection_ms':((first_detect_end-start)+(det_end-direction_end))*1000,
                          'orientation_ms':(direction_end-first_detect_end)*1000,
                          'recognition_ms':(end-det_end)*1000,'total_ms':(end-start)*1000},
                'orientation':orientation,'pipeline':PIPELINE_VERSION,'backend':'CPUExecutionProvider','det_input_shape':shape}
