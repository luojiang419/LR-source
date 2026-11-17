package com.framekeep.inventory;

import android.content.Context;
import android.graphics.Bitmap;
import com.shiying.ocr.PaddleEngine;
import org.json.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.*;

final class PaddleSkuRecognizer {
    static JSONObject recognize(Context context,File image,String rule) throws Exception {
        JSONObject selected;
        try(BufferedReader reader=new BufferedReader(new InputStreamReader(context.getAssets().open("paddle-models/selected.json"),StandardCharsets.UTF_8))){StringBuilder json=new StringBuilder();String line;while((line=reader.readLine())!=null)json.append(line);selected=new JSONObject(json.toString());}
        return recognize(context,image,rule,"paddle-models",selected.getString("detector"),selected.getString("recognizer"));
    }
    static JSONObject recognize(Context context,File image,String rule,String root,String detector,String recognizer) throws Exception {
        long start=System.nanoTime();Bitmap bitmap=SkuRecognizer.decode(image,2400);JSONObject output;
        try{output=PaddleEngine.recognize(context,bitmap,root,detector,recognizer);}finally{bitmap.recycle();}
        JSONArray lines=output.getJSONArray("lines");ArrayList<OcrTextLayout.Line> layout=new ArrayList<>();Map<String,Double> scores=new HashMap<>();
        for(int i=0;i<lines.length();i++){
            JSONObject line=lines.getJSONObject(i);JSONArray points=line.getJSONArray("points");double left=Double.MAX_VALUE,top=Double.MAX_VALUE,right=0,bottom=0;
            for(int j=0;j<points.length();j++){JSONArray point=points.getJSONArray(j);left=Math.min(left,point.getDouble(0));top=Math.min(top,point.getDouble(1));right=Math.max(right,point.getDouble(0));bottom=Math.max(bottom,point.getDouble(1));}
            layout.add(new OcrTextLayout.Line(line.getString("text"),(int)left,(int)top,(int)Math.ceil(right),(int)Math.ceil(bottom)));
            JSONObject parsed=SkuRecognizer.analyze(line.getString("text"),rule,recognizer);JSONArray preferred=parsed.getJSONArray("preferred");
            for(int j=0;j<preferred.length();j++){String sku=preferred.getString(j);scores.put(sku,Math.max(scores.getOrDefault(sku,0.0),line.optDouble("confidence",0)));}
        }
        JSONObject result=SkuRecognizer.analyze(OcrTextLayout.text(layout),rule,recognizer.replace("_rec","").toLowerCase(Locale.ROOT));
        double confidence=scores.getOrDefault(result.optString("sku"),0.0);result.put("confidence",confidence);
        if(result.optBoolean("automatic")&&confidence<.92){result.put("automatic",false);result.put("sku","");}
        result.put("lines",lines).put("milliseconds",(System.nanoTime()-start)/1_000_000).put("timing",output);return result;
    }
}
