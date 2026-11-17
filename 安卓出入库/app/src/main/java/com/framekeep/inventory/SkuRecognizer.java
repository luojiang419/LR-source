package com.framekeep.inventory;

import android.content.Context;
import android.net.*;
import android.graphics.*;
import com.google.mlkit.vision.common.InputImage;
import com.google.mlkit.vision.text.TextRecognition;
import com.google.mlkit.vision.text.latin.TextRecognizerOptions;
import com.google.mlkit.vision.text.chinese.ChineseTextRecognizerOptions;
import com.google.mlkit.vision.text.Text;
import com.google.android.gms.tasks.Tasks;
import org.json.*;
import java.io.*;
import java.net.*;
import java.text.Normalizer;
import java.util.*;
import java.util.concurrent.TimeUnit;
import java.util.regex.*;

final class SkuRecognizer {
    final Context context;
    SkuRecognizer(Context c){context=c;}
    static JSONObject analyze(String raw,String rule,String source) throws Exception {
        SkuCandidates.Analysis result=SkuCandidates.analyze(raw,rule);
        return new JSONObject().put("text",raw).put("source",source).put("candidates",new JSONArray(result.candidates)).put("preferred",new JSONArray(result.preferred)).put("automatic",result.automatic).put("sku",result.sku);
    }
    JSONObject offline(File image,String rule,boolean chinese) throws Exception {
        var recognizer=chinese?TextRecognition.getClient(new ChineseTextRecognizerOptions.Builder().build()):TextRecognition.getClient(TextRecognizerOptions.DEFAULT_OPTIONS);
        try{Bitmap bitmap=decode(image,2400);try{
            Text recognized=Tasks.await(recognizer.process(InputImage.fromBitmap(bitmap,0)),30,TimeUnit.SECONDS);
            ArrayList<OcrTextLayout.Line> lines=new ArrayList<>();
            for(Text.TextBlock block:recognized.getTextBlocks())for(Text.Line line:block.getLines()){
                Rect box=line.getBoundingBox();if(box!=null)lines.add(new OcrTextLayout.Line(line.getText(),box.left,box.top,box.right,box.bottom));
            }
            return analyze(lines.isEmpty()?recognized.getText():OcrTextLayout.text(lines),rule,chinese?"mlkit-chinese-v2":"mlkit-latin-v2");
        }finally{bitmap.recycle();}}finally{recognizer.close();}
    }
    JSONObject recognize(File image,String rule,boolean cloud,String key) throws Exception {
        return recognize(image,rule,cloud,key,context.getSharedPreferences("inventory",0).getString("ocrModel",OcrModels.DEFAULT));
    }
    JSONObject recognize(File image,String rule,boolean cloud,String key,String model) throws Exception {
        OcrModels.require(model);
        JSONObject result;
        if(model.equals("mlkit-chinese")||model.equals("mlkit-latin"))result=offline(image,rule,model.equals("mlkit-chinese"));
        else try{String family=OcrModels.family(model);result=PaddleSkuRecognizer.recognize(context,image,rule,"paddle-models",family+"_det",family+"_rec");}
        catch(Exception|LinkageError paddleUnavailable){result=offline(image,rule,true);result.put("warning","所选模型暂不可用，已使用备用识别");}
        result.put("requestedModel",model);
        if(result.optBoolean("automatic"))return result;
        if(!result.optBoolean("automatic")&&cloud&&online()&&!BuildConfig.OCR_URL.isEmpty()){
            try{JSONObject enhanced=analyze(cloud(image,key),rule,"glm-ocr");if(enhanced.getJSONArray("candidates").length()>0)result=enhanced;}
            catch(Exception unavailable){result.put("warning","在线识别不可用，请核对离线结果");}
        }
        result.put("requestedModel",model);
        return result;
    }
    boolean online(){ConnectivityManager cm=(ConnectivityManager)context.getSystemService(Context.CONNECTIVITY_SERVICE);NetworkCapabilities n=cm.getNetworkCapabilities(cm.getActiveNetwork());return n!=null&&n.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET);}
    String endpoint(String route){String base=BuildConfig.OCR_URL.replaceAll("/+$","").replaceAll("/(?:chat/completions|models)$","");return base+(base.endsWith("/v1")?"/":"/v1/")+route;}
    JSONObject status(String key)throws Exception {
        long started=System.currentTimeMillis();JSONObject response=Net.json("GET",endpoint("models"),key,null,8000);JSONArray models=response.optJSONArray("data");boolean found=false;
        if(models!=null)for(int i=0;i<models.length();i++)if(BuildConfig.OCR_MODEL.equals(models.getJSONObject(i).optString("id")))found=true;
        if(!found)throw new IOException("接口已连接，但未找到指定模型 "+BuildConfig.OCR_MODEL+"，请在 LM Studio 加载该模型");
        return new JSONObject().put("connected",true).put("model",BuildConfig.OCR_MODEL).put("milliseconds",System.currentTimeMillis()-started);
    }
    static Bitmap decode(File file,int max) throws IOException {
        BitmapFactory.Options opts=new BitmapFactory.Options();opts.inJustDecodeBounds=true;BitmapFactory.decodeFile(file.toString(),opts);opts.inSampleSize=1;
        while(Math.max(opts.outWidth,opts.outHeight)/opts.inSampleSize>max*2)opts.inSampleSize*=2;
        opts.inJustDecodeBounds=false;Bitmap b=BitmapFactory.decodeFile(file.toString(),opts);if(b==null)throw new IOException("无法读取图片");
        int w=b.getWidth(),h=b.getHeight();if(Math.max(w,h)>max){float s=(float)max/Math.max(w,h);Bitmap scaled=Bitmap.createScaledBitmap(b,Math.round(w*s),Math.round(h*s),true);if(scaled!=b)b.recycle();b=scaled;}
        try{androidx.exifinterface.media.ExifInterface exif=new androidx.exifinterface.media.ExifInterface(file);int orientation=exif.getAttributeInt(androidx.exifinterface.media.ExifInterface.TAG_ORIENTATION,1);Matrix matrix=new Matrix();if(orientation==6)matrix.postRotate(90);else if(orientation==3)matrix.postRotate(180);else if(orientation==8)matrix.postRotate(270);if(!matrix.isIdentity()){Bitmap rotated=Bitmap.createBitmap(b,0,0,b.getWidth(),b.getHeight(),matrix,true);if(rotated!=b)b.recycle();b=rotated;}}catch(IOException ignored){}
        return b;
    }
    String cloud(File image,String key) throws Exception {
        Bitmap b=decode(image,1800);ByteArrayOutputStream bytes=new ByteArrayOutputStream();b.compress(Bitmap.CompressFormat.JPEG,88,bytes);b.recycle();
        JSONObject img=new JSONObject().put("type","image_url").put("image_url",new JSONObject().put("url","data:image/jpeg;base64,"+android.util.Base64.encodeToString(bytes.toByteArray(),2)));
        JSONObject txt=new JSONObject().put("type","text").put("text","Text Recognition:");
        JSONObject body=new JSONObject().put("model",BuildConfig.OCR_MODEL).put("temperature",0).put("max_tokens",1024).put("messages",new JSONArray().put(new JSONObject().put("role","user").put("content",new JSONArray().put(img).put(txt))));
        JSONObject response=null;
        for(int attempt=0;attempt<2;attempt++){try{response=Net.json("POST",endpoint("chat/completions"),key,body,35000);break;}catch(IOException e){if(attempt==1)throw e;Thread.sleep(350);}}
        Object content=response.getJSONArray("choices").getJSONObject(0).getJSONObject("message").get("content");
        if(content instanceof String)return (String)content;
        StringBuilder s=new StringBuilder();if(content instanceof JSONArray a)for(int i=0;i<a.length();i++)s.append(a.getJSONObject(i).optString("text")).append('\n');return s.toString();
    }
}
