"""有界、本地的检索意图规划。只产生数据条件，不生成或执行用户 SQL。"""
from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import re
import unicodedata

LOCAL_TZ=timezone(timedelta(hours=8),'Asia/Shanghai')
VOCAB={
 'gender':{'female':'女人 女性 女士 女子 女模特 女生 姑娘 小姐姐 女孩子 女的 woman women female','male':'男人 男性 男士 男子 男模特 男生 小哥哥 男的 man men male'},
 'category':{'coat':'外套 大衣 风衣 夹克 西装 羽绒服 棉服 棉袄 coat jacket blazer overcoat',
  'shirt':'衬衫 衬衣 shirt blouse','jeans':'牛仔裤 牛仔长裤 牛仔短裤 jeans',
  'trousers':'长裤 裤子 休闲裤 西裤 裤装 trousers pants','dress':'连衣裙 礼服 dress',
  'skirt':'半身裙 短裙 长裙 skirt','sweater':'毛衣 针织衫 sweater knitwear',
  'tshirt':'t恤 短袖 t-shirt tshirt','shoes':'鞋子 皮鞋 运动鞋 高跟鞋 shoes sneakers','clothing':'衣服 衣裳 上衣 clothing outfit'},
 'color':{'red':'红色 红颜色 鲜红 大红 酒红 枣红 玫红 红 red','blue':'蓝色 蓝颜色 藏蓝 宝蓝 深蓝 浅蓝 牛仔蓝 蓝 blue navy',
  'white':'白色 米白 象牙白 奶白 白 white ivory','black':'黑色 黑 black',
  'green':'绿色 墨绿 草绿 绿 green','yellow':'黄色 金黄 黄 yellow','pink':'粉色 粉红 pink',
  'purple':'紫色 紫 purple','brown':'棕色 咖啡色 褐色 棕 brown','beige':'米色 卡其色 驼色 beige khaki',
  'gray':'灰色 银灰 深灰 浅灰 灰 grey gray','orange':'橙色 橘色 橙 orange'},
 'scene':{'studio':'摄影棚 棚拍 影棚 studio','outdoor':'户外 室外 outdoor','street':'街头 街拍 街道 street',
  'beach':'海边 沙滩 beach','forest':'森林 树林 forest'},
 'action':{'run':'跑步 奔跑 跑动 running','sit':'坐着 坐姿 seated sitting','stand':'站着 站立 站姿 standing'},
 'material':{'linen':'亚麻 麻质 linen','denim':'牛仔布 牛仔面料 denim','cotton':'棉质 纯棉 cotton','leather':'皮革 皮质 leather'},
 'lighting':{'backlit':'逆光 背光 backlit','soft':'柔光 柔和光线'},
}
LABELS={value:aliases.split()[0] for values in VOCAB.values() for value,aliases in values.items()}
ALIASES={alias:(key,value) for key,values in VOCAB.items() for value,aliases in values.items() for alias in aliases.split()}
def _pattern(alias):return r'(?<![a-z])'+re.escape(alias)+r'(?![a-z])' if re.search('[a-z]',alias) else re.escape(alias)
ATOM=re.compile('|'.join(_pattern(alias) for alias in sorted(ALIASES,key=len,reverse=True)))
FILLER=re.compile(r'\b(?:show|find|search|me|please|images|photos|pictures|wearing|wears|wear|a|an|the|with|in|and|of)\b|请帮我|帮我|帮忙|找一找|找一下|搜索一下|搜索|查找|查询|筛选|找出|找到|有没有|给我|我要|我想|那些|这些|所有|一下|一位|一个|一名|一张|以及|同时|并且|身穿|穿着|身着|穿了|穿|着装|搭配|拍摄于|拍摄|拍的|拍|里的|里面|照片|图片|图像|素材|视频|时候|时候的|的|在|有|了|是|和|与|且|为')
SKU=re.compile(r'(?=.*[a-z])(?=.*\d)[a-z0-9_.-]+$',re.I)

def normalized(text):return unicodedata.normalize('NFKC',text).casefold().strip()
def atoms(text):
 folded=unicodedata.normalize('NFKC',text).casefold()
 return [{'key':ALIASES[m.group()][0],'value':ALIASES[m.group()][1],'start':m.start(),'end':m.end()} for m in ATOM.finditer(folded)]
def clothing(text,found=None):
 found=found if found is not None else atoms(text);result=[]
 for atom in found:
  if atom['key']!='category':continue
  colors=[a for a in found if a['key']=='color' and 0<=atom['start']-a['end']<=9
          and not any(b['key'] in ('category','gender') and a['end']<=b['start']<atom['start'] for b in found)
          and not re.search('背景|墙|旁边',text[min(a['end'],atom['start']):max(a['end'],atom['start'])])]
  after=[a for a in found if a['key']=='color' and atom['end']<=a['start']<=atom['end']+6
         and re.fullmatch(r'\s*(?:是|为|颜色是|颜色为|的颜色是|呈|:)\s*',text[atom['end']:a['start']])]
  color=(after[0]['value'] if after else min(colors,key=lambda a:abs(atom['start']-a['end']))['value']) if colors or after else ''
  pair={'category':atom['value'],'color':color}
  if pair not in result:result.append(pair)
 return result
def _month(day,offset):
 number=day.year*12+day.month-1+offset
 return date(number//12,number%12+1,1)
def _window(start,end,label):
 return {'start':start.isoformat(),'end':end.isoformat(),'label':label,
         'from':datetime.combine(start,datetime.min.time(),LOCAL_TZ).timestamp(),
         'to':datetime.combine(end,datetime.min.time(),LOCAL_TZ).timestamp()}

def dates(text,today):
 result=[];spans=[]
 relative=re.compile(r'上上个月|上上月|上一个月|前一个月|上个月|上月|本月|这个月|当月|下个月|下月|大前天|前天|昨天|昨日|今天|今日|明天|本周|这周|上周|下周|今年|去年|前年|明年|最近\s*\d{1,3}\s*天|过去\s*\d{1,3}\s*天|近\s*\d{1,3}\s*天')
 for match in relative.finditer(text):
  word=match.group();start=end=None
  if word in ('昨天','昨日','今天','今日','前天','大前天','明天'):
   start=today+timedelta(days={'昨天':-1,'昨日':-1,'今天':0,'今日':0,'前天':-2,'大前天':-3,'明天':1}[word]);end=start+timedelta(days=1)
  elif '月' in word:
   offset=-2 if word.startswith('上上') else -1 if word.startswith(('上','前')) else 1 if word.startswith('下') else 0
   start=_month(today,offset);end=_month(today,offset+1)
  elif '周' in word:
   start=today-timedelta(days=today.weekday())+timedelta(days=-7 if word.startswith('上') else 7 if word.startswith('下') else 0);end=start+timedelta(days=7)
  elif word in ('今年','去年','前年','明年'):
   year=today.year+{'今年':0,'去年':-1,'前年':-2,'明年':1}[word];start=date(year,1,1);end=date(year+1,1,1)
  else:
   days=int(re.search(r'\d+',word)[0]);
   if days<1:raise ValueError('时间范围至少为 1 天')
   start=today-timedelta(days=days-1);end=today+timedelta(days=1)
  result.append(_window(start,end,word));spans.append(match.span())
 if len(result)==2 and re.fullmatch(r'\s*(到|至|~|—|-)\s*',text[spans[0][1]:spans[1][0]]):
  if result[1]['to']<=result[0]['from']:raise ValueError('结束日期早于开始日期')
  result=[_window(date.fromisoformat(result[0]['start']),date.fromisoformat(result[1]['end']),result[0]['label']+' 至 '+result[1]['label'])]
 # 明确日期/月；省略年份按当前年，第二个日期也可承接第一个年份。
 explicit=re.compile(r'(?<![a-z0-9])(?:(\d{4})[年/.-])?(\d{1,2})(?:月|[/.\-])(\d{1,2})(?:日|号)?(?!\d)|(?<![a-z0-9])(\d{4})[-/](\d{1,2})(?![-/.\d])|(?:(\d{4})年)?(\d{1,2})月')
 year=today.year;entries=[]
 for match in explicit.finditer(text):
  if any(a<=match.start()<b for a,b in spans):continue
  try:
   if match.group(2):
    year=int(match.group(1) or year);start=date(year,int(match.group(2)),int(match.group(3)));end=start+timedelta(days=1)
   else:
    year=int(match.group(4) or match.group(6) or year);start=date(year,int(match.group(5) or match.group(7)),1);end=_month(start,1)
  except ValueError:raise ValueError('搜索中的日期无效') from None
  entries.append((match,_window(start,end,match.group())));spans.append(match.span())
 if len(entries)==2 and re.fullmatch(r'\s*(到|至|~|—|-)\s*',text[entries[0][0].end():entries[1][0].start()]):
  a,b=entries[0][1],entries[1][1]
  if b['to']<=a['from']:raise ValueError('结束日期早于开始日期')
  result.append(_window(date.fromisoformat(a['start']),date.fromisoformat(b['end']),a['label']+' 至 '+b['label']))
 else:result.extend(value for _,value in entries)
 rest=list(text)
 for a,b in spans:rest[a:b]=' '*(b-a)
 return result,''.join(rest)

def plan(text,*,now=None):
 if not isinstance(text,str) or len(text)>300:raise ValueError('搜索文字最多 300 字')
 now=now or datetime.now(LOCAL_TZ)
 today=now.astimezone(LOCAL_TZ).date() if now.tzinfo else now.date()
 return _plan(normalized(text),today.isoformat()).copy()

@lru_cache(maxsize=512)
def _plan(text,today):
 if SKU.fullmatch(text):return {'text':text,'dates':[],'terms':[],'clothes':[],'gender':'','residual':[text],'recognized':False,'media':'','explanation':[],'timezone':'Asia/Shanghai'}
 windows,rest=dates(text,date.fromisoformat(today));frame_target=bool(re.search('帧|关键帧|视频帧',rest));rest=re.sub('关键帧|视频帧|帧|包含了|包含有|包含|带有',' ',rest);found=atoms(rest)
 if windows:rest=re.sub(r'从|到|至|~|—',' ',rest);found=atoms(rest)
 groups=clothing(rest,found);genders=list(dict.fromkeys(a['value'] for a in found if a['key']=='gender'))
 if not groups and genders and re.search('穿|身着|wear|dressed',rest):
  groups=[{'category':'clothing','color':a['value']} for a in found if a['key']=='color']
 remaining=list(rest)
 for atom in found:remaining[atom['start']:atom['end']]=' '*(atom['end']-atom['start'])
 rest_words=re.sub('身上|一件|一条|穿的是',' ',''.join(remaining))
 any_clothes=bool(re.search(r'或者|或是|还是|或|\bor\b',rest_words)) and len(groups)>1
 rest_words=re.sub(r'或者|或是|还是|或|\bor\b',' ',rest_words)
 residual=[part.strip() for part in FILLER.sub(' ',rest_words).split() if part.strip(' ,，。!！?？、;；')]
 residual=[re.sub(r'^[,，。!！?？、;；]+|[,，。!！?？、;；]+$','',part) for part in residual]
 media='video' if frame_target else 'video' if re.search('视频|影片|录像',text) else 'image' if re.search('照片|图片|图像',text) else ''
 # SKU / 文件名沿用原先的精确、前缀和路径检索，不被语义分词破坏。
 recognized=bool(windows or found or frame_target or FILLER.search(text)) and not SKU.fullmatch(text)
 terms=list(dict.fromkeys(a['key']+a['value'] for a in found))
 if any_clothes:terms=[term for term in terms if not term.startswith(('category','color'))]
 explanation=[]
 for window in windows:explanation.append(f"{window['label']}：{window['start']} 至 {(date.fromisoformat(window['end'])-timedelta(days=1)).isoformat()}")
 explanation.extend(LABELS.get(value,value) for value in genders)
 explanation.extend((LABELS.get(group['color'],'')+LABELS.get(group['category'],group['category'])) for group in groups)
 explanation.extend(LABELS.get(a['value'],a['value']) for a in found if a['key'] not in ('category','gender') and (not groups or a['key']!='color'))
 if residual:explanation.append('关键词：'+'、'.join(residual))
 if windows:explanation.append('优先拍摄时间，缺失时用文件修改时间')
 if any_clothes:explanation.append('穿着条件满足其中任一项')
 return {'text':text,'dates':windows,'terms':terms,'clothes':groups,'clothes_mode':'any' if any_clothes else 'all','gender':genders[0] if len(genders)==1 else '',
         'residual':[value for value in residual if value], 'recognized':recognized,'media':media,
         'target':'frames' if frame_target else 'mixed','explanation':list(dict.fromkeys(explanation)),'timezone':'Asia/Shanghai'}

def document(value):
 """从结构化模型结果生成帧级文档和可验证的人物着装关系。"""
 text=' '.join(str(v) for v in [value.get('summary',''),*value.get('attributes',{}).values(),*value.get('custom',{}).values(),*value.get('keywords',[])])
 subjects=value.get('subjects') or []
 text+=' '+' '.join(str(item.get(key,'')) for subject in subjects for item in subject.get('clothing',[]) for key in ('category','color','description'))
 found=atoms(text);canonical={a['key']+a['value'] for a in found};wear=[]
 if 'subjects' in value:canonical={term for term in canonical if not term.startswith('gender')}
 for person,subject in enumerate(subjects):
  gender=subject.get('gender','unknown')
  canonical.add('gender'+gender)
  for garment,item in enumerate(subject.get('clothing',[])):
   categories={a['value'] for a in atoms(item.get('category','')) if a['key']=='category'}
   colors={a['value'] for a in atoms(item.get('color','')) if a['key']=='color'} or {''}
   for category in categories:
    for color in colors:wear.append((person,garment,gender,category,color))
 if not subjects:
  summary=normalized(value.get('summary',''));people=[a for a in atoms(summary) if a['key']=='gender']
  for person,subject in enumerate(people):
   end=people[person+1]['start'] if person+1<len(people) else len(summary)
   segment=summary[subject['start']:end];parts=[]
   for verb in re.finditer('身穿|穿着|身着|穿|着装|搭配|wearing|wears|dressed',segment):
    parts.append(re.split('背景|手持|拿着|手里|手中|旁边|背后|挂着|摆放',segment[verb.end():])[0])
   # 兼容“穿红外套的女性”，只取最近穿着动词之后的前缀。
   prefix=summary[(people[person-1]['end'] if person else 0):subject['start']]
   verbs=list(re.finditer('身穿|穿着|身着|穿|wearing|dressed',prefix))
   if verbs and re.search(r'的\s*$',prefix):parts.append(prefix[verbs[-1].end():])
   clothes=[]
   for part in parts:
    for item in clothing(part):
     if item not in clothes:clothes.append(item)
   for garment,item in enumerate(clothes):
    wear.append((person,garment,subject['value'],item['category'],item['color']))
  # 无人商品图的单一品类/颜色可兼容旧解析；有人但无着装证据时不交叉拼接属性。
  if 'subjects' in value or not any(a['key']=='gender' for a in found):
   attrs=value.get('attributes',{});categories={a['value'] for a in atoms(attrs.get('category','')) if a['key']=='category'};colors={a['value'] for a in atoms(attrs.get('color','')) if a['key']=='color'}
   if len(categories)==len(colors)==1:wear.append((-1,0,'unknown',next(iter(categories)),next(iter(colors))))
 expanded=set(wear)
 for person,garment,gender,category,color in wear:
  canonical.update(['category'+category,'gender'+gender])
  if color:canonical.add('color'+color)
  if category=='jeans':expanded.add((person,garment,gender,'trousers',color));canonical.add('categorytrousers')
  if category!='shoes':expanded.add((person,garment,gender,'clothing',color));canonical.add('categoryclothing')
 return text,canonical,sorted(expanded)
