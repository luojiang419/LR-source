package com.framekeep.inventory;

import android.Manifest;
import android.content.*;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.media.*;
import android.net.Uri;
import android.os.*;
import android.view.*;
import android.webkit.*;
import android.widget.FrameLayout;
import androidx.activity.ComponentActivity;
import androidx.camera.core.*;
import androidx.camera.lifecycle.ProcessCameraProvider;
import androidx.camera.view.PreviewView;
import androidx.core.content.ContextCompat;
import androidx.core.content.FileProvider;
import androidx.webkit.WebViewAssetLoader;
import com.google.common.util.concurrent.ListenableFuture;
import org.json.*;
import java.io.*;
import java.net.*;
import java.nio.file.Files;
import java.time.OffsetDateTime;
import java.util.*;
import java.util.concurrent.*;

public final class MainActivity extends ComponentActivity {
    WebView web;PreviewView preview;ProcessCameraProvider provider;Camera camera;ImageCapture capture;
    View captureFlashView;long captureFlashGeneration=0;
    InventoryStore store;SharedPreferences prefs;JSONObject draft;PhoneServer phone;
    static final String XLSX_MIME="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
    static final int SAVE_EXPORT=42;
    File pendingExport;boolean exportInProgress=false;
    final ExecutorService worker=Executors.newSingleThreadExecutor();
    final ExecutorService networkWorker=Executors.newSingleThreadExecutor();
    final BroadcastReceiver ocrUpdates=new BroadcastReceiver(){@Override public void onReceive(Context context,Intent intent){try{JSONObject data=new JSONObject().put("summary",store.summary()).put("usbConnected",UsbTransferService.running);String id=intent.getStringExtra("id");if(id!=null&&!id.isEmpty())try{data.put("record",store.record(id));}catch(Exception deleted){}emit("recordUpdated",data);}catch(Exception ignored){}}};
    final Handler continuationHandler=new Handler(Looper.getMainLooper());
    final NextInboundCapture nextInbound=new NextInboundCapture();
    boolean foreground=false;long inboundSavedAt=0,cameraGeneration=0;
    boolean busy=false,recognizing=false,discardConfirm=false,torch=false,clearing=false;long recognitionGeneration=0;volatile long dataGeneration=0;String pickerKind="product";MediaPlayer voice,captureVoice;ToneGenerator tone;
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);prefs=getSharedPreferences("inventory",0);
        if(state!=null&&state.getString("pendingExport")!=null){pendingExport=new File(new File(getFilesDir(),"exports"),state.getString("pendingExport"));exportInProgress=true;}
        if(!prefs.contains("device"))prefs.edit().putString("device",UUID.randomUUID().toString()).putString("phoneToken",UUID.randomUUID().toString().replace("-","")).commit();
        store=new InventoryStore(this);try{draft=new JSONObject(prefs.getString("draft","{}"));}catch(Exception e){draft=new JSONObject();}
        systemTheme(prefs.getString("theme","dark"));
        FrameLayout root=new FrameLayout(this);preview=new PreviewView(this);preview.setImplementationMode(PreviewView.ImplementationMode.COMPATIBLE);root.addView(preview,new FrameLayout.LayoutParams(-1,-1));
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);
        web=new WebView(this);web.setBackgroundColor(Color.TRANSPARENT);root.addView(web,new FrameLayout.LayoutParams(-1,-1));setContentView(root);
        captureFlashView=new View(this);captureFlashView.setBackgroundColor(Color.WHITE);captureFlashView.setVisibility(View.GONE);captureFlashView.setClickable(false);captureFlashView.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);root.addView(captureFlashView,new FrameLayout.LayoutParams(-1,-1));
        web.getSettings().setJavaScriptEnabled(true);web.getSettings().setDomStorageEnabled(true);web.getSettings().setAllowFileAccess(false);web.getSettings().setAllowContentAccess(false);web.getSettings().setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);web.getSettings().setSupportZoom(false);
        WebViewAssetLoader loader=new WebViewAssetLoader.Builder().addPathHandler("/assets/",new WebViewAssetLoader.AssetsPathHandler(this)).addPathHandler("/images/",path->{try{String[] p=path.split("/");File f=store.image(p[0],p[1]);return new WebResourceResponse("image/jpeg",null,new FileInputStream(f));}catch(Exception e){return new WebResourceResponse("text/plain","utf-8",404,"Not found",null,new ByteArrayInputStream(new byte[0]));}}).addPathHandler("/draft/",path->{try{if(!path.equals("product.jpg")&&!path.equals("label.jpg"))throw new IOException();return new WebResourceResponse("image/jpeg",null,new FileInputStream(draftFile(path.startsWith("product")?"product":"label")));}catch(Exception e){return new WebResourceResponse("text/plain","utf-8",404,"Not found",null,new ByteArrayInputStream(new byte[0]));}}).build();
        web.setWebViewClient(new WebViewClient(){@Override public WebResourceResponse shouldInterceptRequest(WebView v,WebResourceRequest r){return loader.shouldInterceptRequest(r.getUrl());}@Override public boolean shouldOverrideUrlLoading(WebView v,WebResourceRequest r){return true;}});
        web.setWebChromeClient(new WebChromeClient(){@Override public boolean onJsConfirm(WebView v,String url,String message,JsResult result){new android.app.AlertDialog.Builder(MainActivity.this).setTitle("确认操作").setMessage(message).setPositiveButton("放弃",(d,which)->result.confirm()).setNegativeButton("保留",(d,which)->result.cancel()).setOnCancelListener(d->result.cancel()).show();return true;}});
        web.addJavascriptInterface(new Bridge(),"Native");web.loadUrl("https://appassets.androidplatform.net/assets/index.html");
        getOnBackPressedDispatcher().addCallback(this,new androidx.activity.OnBackPressedCallback(true){@Override public void handleOnBackPressed(){nextInbound.cancel();emit("back",new JSONObject());}});
        androidx.core.content.ContextCompat.registerReceiver(this,ocrUpdates,new IntentFilter(OcrWorker.UPDATE),androidx.core.content.ContextCompat.RECEIVER_NOT_EXPORTED);
        if(store.cleanupPending())beginClear("recovery");else worker.execute(()->{try{OcrQueue.recover(this,store);}catch(Exception ignored){}});
    }
    File draftFile(String kind){File d=new File(getFilesDir(),"draft");d.mkdirs();return new File(d,kind+".jpg");}
    void systemTheme(String theme){boolean light=theme.equals("light")||theme.equals("pure-white");int color=Color.parseColor(theme.equals("light")?"#f6eee4":theme.equals("pure-white")?"#fafafa":theme.equals("hardcore")?"#171819":"#1a1a1a");getWindow().setStatusBarColor(color);getWindow().setNavigationBarColor(color);getWindow().getDecorView().setSystemUiVisibility(light?View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR|View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR:0);}
    void persistDraft(){prefs.edit().putString("draft",draft.toString()).commit();}
    JSONObject settings()throws Exception{return new JSONObject().put("theme",prefs.getString("theme","dark")).put("reduced",prefs.getBoolean("reduced",false)).put("rules",prefs.getString("rules","LC")).put("cloud",prefs.getBoolean("cloud",true)).put("url",prefs.getString("url","")).put("token",prefs.getString("token","")).put("sound",prefs.getBoolean("sound",true)).put("autoSync",prefs.getBoolean("autoSync",true)).put("ocrModel",prefs.getString("ocrModel",OcrModels.DEFAULT));}
    void emit(String event,JSONObject data){runOnUiThread(()->{if(!isFinishing()&&!isDestroyed())web.evaluateJavascript("window.onNative&&window.onNative("+JSONObject.quote(event)+","+data+")",null);});}
    void result(String id,Object data,String error){try{emit("reply",new JSONObject().put("id",id).put("data",data==null?JSONObject.NULL:data).put("error",error==null?JSONObject.NULL:error));}catch(Exception ignored){}}
    JSONObject state()throws Exception{return new JSONObject().put("summary",clearing?new JSONObject().put("in",0).put("out",0).put("pending",0).put("total",0):store.summary()).put("draft",draft).put("settings",settings()).put("clearing",clearing).put("usbConnected",UsbTransferService.running).put("cleanupPending",clearing||store.cleanupPending());}
    void stateEvent(){runOnUiThread(()->{if(isFinishing()||isDestroyed())return;try{emit("state",state());}catch(Exception ignored){}});}
    final class Bridge {
        @JavascriptInterface public void call(String action,String input,String id){runOnUiThread(()->dispatch(action,input,id));}
    }
    void dispatch(String action,String input,String id){
        try {
            JSONObject b=new JSONObject(input);
            if((clearing||store.cleanupPending())&&!Arrays.asList("state","list","clearRecords","stopCamera","stopServe","cancelNextCapture").contains(action))throw new IOException("请先完成本机记录清理，再开始新的操作");
            switch(action){
                case "state":result(id,state(),null);return;
                case "list":worker.execute(()->{try{result(id,new JSONObject().put("rows",store.list(b.optString("mode"),b.optString("query"),b.optInt("offset"),60,false)).put("summary",store.summary()),null);}catch(Exception e){result(id,null,e.getMessage());}});return;
                case "trashList":worker.execute(()->{try{result(id,new JSONObject().put("rows",store.trashList(b.optString("mode"),b.optString("query"),b.optInt("offset"),60)).put("summary",store.summary()),null);}catch(Exception e){result(id,null,e.getMessage());}});return;
                case "deleteRecord":case "restoreRecord":
                    worker.execute(()->{try{if(action.equals("deleteRecord"))store.trash(b.getString("id"));else{store.restore(b.getString("id"));JSONObject restored=store.record(b.getString("id"));if(restored.optString("ocr_status").equals("pending"))OcrQueue.enqueue(this,restored);}result(id,new JSONObject().put("summary",store.summary()),null);}catch(Exception e){result(id,null,e.getMessage());}});return;
                case "record":worker.execute(()->{try{result(id,store.record(b.getString("id")),null);}catch(Exception e){result(id,null,e.getMessage());}});return;
                case "confirmRecord":case "updateRecordSku":case "retryRecord":
                    worker.execute(()->{try{JSONObject r=action.equals("confirmRecord")?store.confirmRecord(b.getString("id"),b.getInt("revision"),b.getString("sku")):action.equals("updateRecordSku")?store.updateRecordSku(b.getString("id"),b.getInt("revision"),b.getString("sku")):store.retryRecord(b.getString("id"),b.getInt("revision"),prefs.getString("ocrModel",OcrModels.DEFAULT));if(action.equals("retryRecord"))OcrQueue.enqueue(this,r);else if(prefs.getBoolean("autoSync",true)&&!prefs.getString("url","").isEmpty())SyncWorker.enqueue(this);result(id,r,null);OcrWorker.notifyRecord(this,r.getString("id"));}catch(Exception e){result(id,null,e.getMessage());}});return;
                case "enqueueDraft":saveCaptured(id);return;
                case "settings":
                    String ocrModel=OcrModels.require(b.optString("ocrModel",prefs.getString("ocrModel",OcrModels.DEFAULT)));
                    String theme=b.optString("theme","dark");if(!Arrays.asList("dark","light","pure-white","hardcore").contains(theme))throw new IOException("主题无效");
                    String rules=b.optString("rules","LC").trim().toUpperCase(Locale.ROOT);if(rules.length()>300||!rules.matches("[A-Z0-9._/,;，；\\s-]*"))throw new IOException("请填写有效的规则文字，以逗号分隔");
                    String url=b.optString("url").trim();if(!url.isEmpty())url=Net.base(url);
                    prefs.edit().putString("ocrModel",ocrModel).putString("theme",theme).putBoolean("reduced",b.optBoolean("reduced")).putString("rules",rules).putBoolean("cloud",b.optBoolean("cloud",true)).putString("url",url).putString("token",b.optString("token").trim()).putBoolean("sound",b.optBoolean("sound",true)).putBoolean("autoSync",b.optBoolean("autoSync",true)).commit();systemTheme(theme);result(id,settings(),null);return;
                case "start":
                    if(busy)throw new IOException("正在处理当前拍摄");if(draft.has("id"))throw new IOException("请先继续或取消未完成记录");
                    String mode=b.optString("mode");if(!mode.equals("in")&&!mode.equals("out"))throw new IOException("请选择出入库模式");
                    nextInbound.cancel();
                    draft=new JSONObject().put("id",UUID.randomUUID().toString()).put("mode",mode).put("stage","product").put("at",OffsetDateTime.now().toString());persistDraft();startCamera();result(id,draft,null);return;
                case "resume":startCamera();result(id,draft,null);return;
                case "capture":takePhoto(id);return;
                case "torch":if(camera==null||!camera.getCameraInfo().hasFlashUnit())throw new IOException("当前相机无可用闪光灯");torch=!torch;camera.getCameraControl().enableTorch(torch);result(id,new JSONObject().put("on",torch),null);return;
                case "retake":
                    if(busy)throw new IOException("请等待当前处理完成");String kind=b.optString("kind","label");if(!kind.equals("label")&&!kind.equals("product"))throw new IOException("重拍类型无效");draft.remove("recognition");draft.put("stage",kind);persistDraft();startCamera();result(id,draft,null);return;
                case "import":pickerKind=b.optString("kind","product");if(!pickerKind.equals("product")&&!pickerKind.equals("label"))throw new IOException("图片类型无效");Intent picker=new Intent(Intent.ACTION_OPEN_DOCUMENT).setType("image/*").addCategory(Intent.CATEGORY_OPENABLE);startActivityForResult(picker,41);result(id,new JSONObject(),null);return;
                case "recognize":recognize();result(id,new JSONObject(),null);return;
                case "ocrStatus":networkWorker.execute(()->{try{result(id,new SkuRecognizer(MainActivity.this).status(SecretVault.ocrKey(MainActivity.this)),null);}catch(Exception e){result(id,null,"OCR 连接未完成："+e.getMessage());}});return;
                case "save":saveRecord(b.optString("sku"),id);return;
                case "cancel":confirmDiscard(id);return;
                case "stopCamera":stopCamera();result(id,new JSONObject(),null);return;
                case "cancelNextCapture":nextInbound.cancel();result(id,new JSONObject(),null);return;
                case "export":
                    String scope=b.optString("scope","all");
                    if(!scope.equals("all")&&!scope.equals("dates"))throw new IOException("请选择导出全部或日期范围");
                    ExportRange range=scope.equals("dates")?ExportRange.dates(b.optString("start"),b.optString("end"),java.time.ZoneId.systemDefault()):ExportRange.all();
                    String destination=b.optString("destination","local");
                    if(!destination.equals("local")&&!destination.equals("share"))throw new IOException("请选择保存到本地或分享");
                    if(exportInProgress)throw new IOException("请先完成或取消当前导出");
                    exportBook(scope.equals("all")?"":b.optString("mode"),"",range,destination,id);return;
                case "clearRecords":
                    if(busy||clearing||exportInProgress)throw new IOException("正在处理当前操作，请稍后再清理");
                    new android.app.AlertDialog.Builder(this).setTitle("清空全部扫描记录与图片？").setMessage("将永久删除本机全部入库/出库记录、产品图、SKU 标签图、未完成拍摄及应用内导出缓存。\n\n请先导出需要保留的表格。电脑端已同步资料及另存的表格保留。").setNegativeButton("取消",(d,which)->result(id,new JSONObject(),null)).setPositiveButton("清空全部",(d,which)->{if(busy)result(id,null,"正在处理当前操作，请稍后再清理");else beginClear(id);}).setOnCancelListener(d->result(id,new JSONObject(),null)).show();return;
                case "sync":networkWorker.execute(()->{try{result(id,sync(),null);stateEvent();}catch(Exception e){result(id,null,e.getMessage());}});return;
                case "serve":if(phone==null)phone=new PhoneServer(store,prefs.getString("phoneToken",""),null,8792,()->{},recordId->OcrWorker.notifyRecord(this,recordId));result(id,phoneState(),null);return;
                case "stopServe":if(phone!=null){phone.stop();phone=null;}result(id,new JSONObject(),null);return;
                default:throw new IOException("未知操作");
            }
        }catch(Exception e){result(id,null,e.getMessage());}
    }
    void confirmDiscard(String request)throws Exception {
        if(discardConfirm)throw new IOException("请先处理当前确认窗口");
        if(busy&&!recognizing)throw new IOException("正在拍摄或保存，请稍后放弃");
        String draftId=draft.optString("id");
        if(draftId.isEmpty())throw new IOException("当前没有未完成记录");
        discardConfirm=true;
        new android.app.AlertDialog.Builder(this).setTitle("放弃本条未完成记录？")
            .setMessage("将删除本条已拍摄的产品和标签照片，已保存的出入库记录保留。")
            .setNegativeButton("保留",(d,which)->finishDiscard(request,false,draftId))
            .setPositiveButton("放弃",(d,which)->finishDiscard(request,true,draftId))
            .setOnCancelListener(d->finishDiscard(request,false,draftId)).show();
    }
    void finishDiscard(String request,boolean discard,String draftId){
        discardConfirm=false;
        try{
            if(discard){
                if(!draftId.equals(draft.optString("id")))throw new IOException("记录已变化，请重新核对");
                if(busy&&!recognizing)throw new IOException("正在拍摄或保存，请稍后放弃");
                // 识别只读取文件；使旧回调失效，避免取消后重新弹出或自动保存。
                recognitionGeneration++;recognizing=false;busy=false;nextInbound.cancel();stopCamera();
                draftFile("product").delete();draftFile("label").delete();draft=new JSONObject();persistDraft();stateEvent();
            }
            result(request,new JSONObject().put("cancelled",discard),null);
        }catch(Exception e){result(request,null,e.getMessage());}
    }
    void startCamera(){
        if(!draft.has("id"))return;
        if(ContextCompat.checkSelfPermission(this,Manifest.permission.CAMERA)!=PackageManager.PERMISSION_GRANTED){requestPermissions(new String[]{Manifest.permission.CAMERA},40);return;}
        long ticket=++cameraGeneration;
        ListenableFuture<ProcessCameraProvider> f=ProcessCameraProvider.getInstance(this);
        f.addListener(()->{if(ticket!=cameraGeneration||isDestroyed())return;try{provider=f.get();provider.unbindAll();Preview p=new Preview.Builder().build();p.setSurfaceProvider(preview.getSurfaceProvider());capture=new ImageCapture.Builder().setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY).setTargetResolution(new android.util.Size(1920,2560)).build();camera=provider.bindToLifecycle(this,CameraSelector.DEFAULT_BACK_CAMERA,p,capture);torch=false;emit("camera",new JSONObject().put("ready",true).put("flash",camera.getCameraInfo().hasFlashUnit()));}catch(Exception e){try{emit("error",new JSONObject().put("message","相机启动失败，请检查权限或改用相册："+e.getMessage()));}catch(Exception ignored){}}},ContextCompat.getMainExecutor(this));
    }
    void stopCamera(){cameraGeneration++;if(camera!=null)camera.getCameraControl().enableTorch(false);if(provider!=null)provider.unbindAll();camera=null;capture=null;torch=false;}
    @Override public void onRequestPermissionsResult(int code,String[] permissions,int[] grants){super.onRequestPermissionsResult(code,permissions,grants);if(code==40){if(grants.length>0&&grants[0]==0)startCamera();else try{emit("error",new JSONObject().put("message","请在系统设置允许相机权限，也可从相册选择图片"));}catch(Exception ignored){}}}
    void takePhoto(String request){
        if(capture==null||busy){result(request,null,"相机尚未就绪或正在识别");return;}
        String kind=draft.optString("stage");if(!kind.equals("product")&&!kind.equals("label")){result(request,null,"当前照片已拍摄，请确认或重拍");return;}
        busy=true;pulseCaptureFlash();try{emit("capturing",new JSONObject().put("kind",kind).put("nativeFlash",true));}catch(Exception ignored){}
        File file=draftFile(kind);capture.takePicture(new ImageCapture.OutputFileOptions.Builder(file).build(),ContextCompat.getMainExecutor(this),new ImageCapture.OnImageSavedCallback(){
            @Override public void onImageSaved(ImageCapture.OutputFileResults r){busy=false;try{photoReady(kind);result(request,draft,null);}catch(Exception e){result(request,null,e.getMessage());}}
            @Override public void onError(ImageCaptureException e){busy=false;result(request,null,"拍摄失败，请重试");}
        });
        playCaptureVoice();
    }
    void stopCaptureVoice(){if(captureVoice!=null){captureVoice.release();captureVoice=null;}}
    void pulseCaptureFlash(){
        long ticket=++captureFlashGeneration;captureFlashView.animate().cancel();captureFlashView.setAlpha(prefs.getBoolean("reduced",false)?.25f:.82f);captureFlashView.setVisibility(View.VISIBLE);
        captureFlashView.postDelayed(()->{if(ticket!=captureFlashGeneration||isDestroyed())return;captureFlashView.animate().alpha(0).setDuration(180).setInterpolator(new android.view.animation.PathInterpolator(.22f,1f,.36f,1f)).withEndAction(()->{if(ticket==captureFlashGeneration)captureFlashView.setVisibility(View.GONE);}).start();},80);
    }
    void playCaptureVoice(){
        stopCaptureVoice();if(!prefs.getBoolean("sound",true))return;
        // 拍照已发起；异步准备短语音，不让音频初始化拖慢快门或闪白反馈。
        try{if(voice!=null){voice.release();voice=null;}MediaPlayer player=new MediaPlayer();captureVoice=player;
            player.setAudioStreamType(AudioManager.STREAM_MUSIC);
            player.setOnPreparedListener(mp->{if(captureVoice!=mp)return;if(isDestroyed()||!prefs.getBoolean("sound",true)){stopCaptureVoice();return;}try{mp.start();}catch(Exception unavailable){stopCaptureVoice();}});
            player.setOnCompletionListener(mp->{if(captureVoice==mp)stopCaptureVoice();});
            player.setOnErrorListener((mp,what,extra)->{if(captureVoice==mp)stopCaptureVoice();return true;});
            try(android.content.res.AssetFileDescriptor asset=getAssets().openFd("capture.mp3")){player.setDataSource(asset.getFileDescriptor(),asset.getStartOffset(),asset.getLength());}
            player.prepareAsync();continuationHandler.postDelayed(()->{if(captureVoice==player)stopCaptureVoice();},3000);
        }catch(Exception unavailable){stopCaptureVoice();}
    }
    void photoReady(String kind)throws Exception {
        draft.put(kind,true);draft.put("stage",kind.equals("product")?"label":"review");persistDraft();
        if(kind.equals("label")){stopCamera();saveCaptured("capture-save");}else emit("draft",draft);
    }
    @Override protected void onActivityResult(int code,int status,Intent data){super.onActivityResult(code,status,data);if(code==SAVE_EXPORT){saveExportResult(status,data);return;}if(code!=41||status!=RESULT_OK||data==null)return;String kind=pickerKind;busy=true;worker.execute(()->{try{
        File dest=draftFile(kind),tmp=new File(dest+".import");try(InputStream in=getContentResolver().openInputStream(data.getData());OutputStream out=new FileOutputStream(tmp)){byte[] b=new byte[65536];int n;long total=0;while((n=in.read(b))!=-1){total+=n;if(total>30_000_000)throw new IOException("请选择小于 30MB 的图片");out.write(b,0,n);}}
        android.graphics.Bitmap image=SkuRecognizer.decode(tmp,2400);try(OutputStream out=new FileOutputStream(dest)){image.compress(android.graphics.Bitmap.CompressFormat.JPEG,92,out);}finally{image.recycle();tmp.delete();}
        runOnUiThread(()->{busy=false;try{photoReady(kind);}catch(Exception e){result("import",null,e.getMessage());}});
    }catch(Exception e){runOnUiThread(()->{busy=false;try{emit("error",new JSONObject().put("message","导入失败："+e.getMessage()));}catch(Exception ignored){}});}});}
    void recognize(){
        if(busy)return;if(!draftFile("product").isFile()||!draftFile("label").isFile())return;busy=true;recognizing=true;long ticket=++recognitionGeneration;emit("recognizing",new JSONObject());
        worker.execute(()->{try{String key="";boolean cloud=prefs.getBoolean("cloud",true);try{if(cloud)key=SecretVault.ocrKey(this);}catch(Exception unavailable){cloud=false;}JSONObject o=new SkuRecognizer(this).recognize(draftFile("label"),prefs.getString("rules","LC"),cloud,key);
            runOnUiThread(()->{if(ticket!=recognitionGeneration)return;recognizing=false;busy=false;try{if(discardConfirm)o.put("automatic",false);draft.put("recognition",o);draft.put("stage","review");persistDraft();emit("recognized",o);if(o.optBoolean("automatic"))saveRecord(o.optString("sku"),"automatic");}catch(Exception e){result("recognize",null,e.getMessage());}});
        }catch(Exception e){runOnUiThread(()->{if(ticket!=recognitionGeneration)return;recognizing=false;busy=false;try{draft.put("stage","review");persistDraft();emit("recognized",new JSONObject().put("automatic",false).put("candidates",new JSONArray()).put("warning","未能识别，请手动填写款号或重拍"));}catch(Exception ignored){}});}});
    }
    void saveCaptured(String request){saveRecordInternal("",request,true);}
    void saveRecord(String sku,String request){saveRecordInternal(sku,request,false);}
    void saveRecordInternal(String sku,String request,boolean captured){
        if(busy){result(request,null,"正在处理，请稍后");return;}if(!draft.has("id")){result(request,null,"当前没有待保存记录");return;}
        busy=true;String requestedModel=prefs.getString("ocrModel",OcrModels.DEFAULT);JSONObject snapshot;try{snapshot=new JSONObject(draft.toString());}catch(Exception e){busy=false;return;}
        worker.execute(()->{try{JSONObject o=snapshot.optJSONObject("recognition");JSONObject r=captured?store.saveCaptured(snapshot.getString("id"),snapshot.getString("mode"),draftFile("product"),draftFile("label"),snapshot.getString("at"),requestedModel):store.save(snapshot.getString("id"),snapshot.getString("mode"),sku,draftFile("product"),draftFile("label"),o==null?"":o.optString("text"),o==null?"manual":o.optString("source"),snapshot.getString("at"));
            runOnUiThread(()->{busy=false;draft=new JSONObject();persistDraft();stopCamera();emit("saved",r);result(request,r,null);stateEvent();
                if(r.optString("mode").equals("in")){long token=nextInbound.begin();inboundSavedAt=SystemClock.uptimeMillis();success("in",()->{nextInbound.audioFinished(token);continueInbound();});}
                else success("out",null);
            });
            if(captured)OcrQueue.enqueue(this,r);else if(prefs.getBoolean("autoSync",true)&&!prefs.getString("url","").isEmpty())SyncWorker.enqueue(this);
        }catch(Exception e){runOnUiThread(()->{busy=false;result(request,null,"保存失败："+e.getMessage());});}});
    }
    void success(String mode,Runnable after){
        stopCaptureVoice();
        boolean[] completed={false};Runnable finish=()->{if(completed[0])return;completed[0]=true;if(after!=null)after.run();};
        if(!prefs.getBoolean("sound",true)){finish.run();return;}
        try{if(tone==null)tone=new ToneGenerator(AudioManager.STREAM_MUSIC,65);tone.startTone(ToneGenerator.TONE_PROP_ACK,140);
            if(voice!=null)voice.release();MediaPlayer player=new MediaPlayer();voice=player;
            player.setOnCompletionListener(mp->{if(voice==mp){voice=null;mp.release();}finish.run();});
            player.setOnErrorListener((mp,what,extra)->{if(voice==mp){voice=null;mp.release();}finish.run();return true;});
            try(android.content.res.AssetFileDescriptor a=getAssets().openFd(mode.equals("in")?"in.mp3":"out.mp3")){player.setDataSource(a.getFileDescriptor(),a.getStartOffset(),a.getLength());}
            player.setAudioStreamType(AudioManager.STREAM_MUSIC);player.prepare();int duration=player.getDuration();player.start();
            continuationHandler.postDelayed(()->{if(!completed[0]){if(voice==player){voice=null;player.release();}finish.run();}},Math.min(5000,Math.max(1000,duration+800)));
        }catch(Exception unavailable){if(voice!=null){voice.release();voice=null;}finish.run();}
    }
    void continueInbound(){
        long token=nextInbound.token();long delay=Math.max(0,250-(SystemClock.uptimeMillis()-inboundSavedAt));
        continuationHandler.postDelayed(()->{
            if(!foreground||isFinishing()||isDestroyed()||busy||clearing||draft.has("id"))return;
            if(store.cleanupPending()||!nextInbound.take(token,true))return;
            try{draft=new JSONObject().put("id",UUID.randomUUID().toString()).put("mode","in").put("stage","product").put("at",OffsetDateTime.now().toString());persistDraft();emit("nextCapture",draft);startCamera();stateEvent();}
            catch(Exception e){try{emit("error",new JSONObject().put("message","下一件拍摄未能启动，请返回后继续："+e.getMessage()));}catch(Exception ignored){}}
        },delay);
    }
    JSONObject sync()throws Exception {InventoryStore connection=new InventoryStore(this);try{return InventorySync.run(this,connection,count->emit("syncProgress",new JSONObject().put("count",count)));}finally{connection.close();}}
    JSONObject phoneState()throws Exception {
        JSONArray addresses=new JSONArray();for(NetworkInterface nic:Collections.list(NetworkInterface.getNetworkInterfaces()))for(InetAddress a:Collections.list(nic.getInetAddresses()))if(a instanceof Inet4Address&&!a.isLoopbackAddress())addresses.put("http://"+a.getHostAddress()+":8792");
        return new JSONObject().put("addresses",addresses).put("token",prefs.getString("phoneToken","")).put("device",store.device);
    }
    void beginClear(String request){
        stopCaptureVoice();
        nextInbound.cancel();
        busy=true;clearing=true;dataGeneration++;stopCamera();if(phone!=null){phone.stop();phone=null;}emit("clearing",new JSONObject());
        worker.execute(()->{try{
            int count=store.clearAll(new File(getFilesDir(),"draft"),new File(getFilesDir(),"exports"));
            if(!prefs.edit().putString("draft","{}").remove("lastSyncTarget").commit())throw new IOException("未完成记录状态未能清除，请重试");
            store.completeClear();
            runOnUiThread(()->{draft=new JSONObject();web.clearCache(true);busy=false;clearing=false;stateEvent();try{emit("cleared",new JSONObject().put("records",count));result(request,new JSONObject().put("cleared",true),null);}catch(Exception ignored){}});
        }catch(Exception e){runOnUiThread(()->{busy=false;clearing=false;stateEvent();result(request,null,"清理未全部完成："+e.getMessage()+"。请重试清理，完成前暂停新扫描。");});}});
    }
    @Override protected void onSaveInstanceState(Bundle state){
        if(pendingExport!=null)state.putString("pendingExport",pendingExport.getName());
        super.onSaveInstanceState(state);
    }
    void exportNotice(String status,String message){try{emit("exportResult",new JSONObject().put("status",status).put("message",message));}catch(Exception ignored){}}
    void saveExportResult(int status,Intent data){
        File source=pendingExport;pendingExport=null;
        if(status!=RESULT_OK||data==null||data.getData()==null){
            exportInProgress=false;if(source!=null)source.delete();exportNotice("cancelled","已取消本地保存");return;
        }
        if(source==null||!source.isFile()){exportInProgress=false;exportNotice("error","导出文件已失效，请重新导出");return;}
        Uri destination=data.getData();
        worker.execute(()->{try{
            try(InputStream in=new FileInputStream(source);OutputStream out=getContentResolver().openOutputStream(destination,"wt")){
                if(out==null)throw new IOException("无法打开所选保存位置");
                byte[] buffer=new byte[65536];int n;while((n=in.read(buffer))!=-1)out.write(buffer,0,n);out.flush();
            }
            exportNotice("saved","表格已保存到所选位置");
        }catch(Exception e){exportNotice("error","本地保存失败，请重新导出："+e.getMessage());}
        finally{source.delete();runOnUiThread(()->exportInProgress=false);}});
    }
    void exportBook(String mode,String query,ExportRange range,String destination,String request){
        exportInProgress=true;
        worker.execute(()->{File target=new File(new File(getFilesDir(),"exports"),"SHIYING 库存管理-"+System.currentTimeMillis()+".xlsx");try{
            target.getParentFile().mkdirs();Workbook.write(target,store,mode,query,range);
            runOnUiThread(()->{try{
                if(destination.equals("local")){
                    pendingExport=target;
                    Intent intent=new Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType(XLSX_MIME).putExtra(Intent.EXTRA_TITLE,target.getName());
                    startActivityForResult(intent,SAVE_EXPORT);
                }else{
                    Uri uri=FileProvider.getUriForFile(this,getPackageName()+".files",target);
                    Intent intent=new Intent(Intent.ACTION_SEND).setType(XLSX_MIME).putExtra(Intent.EXTRA_STREAM,uri).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                    intent.setClipData(ClipData.newRawUri("出入库表格",uri));
                    startActivity(Intent.createChooser(intent,"分享出入库表格"));exportInProgress=false;
                }
                result(request,new JSONObject().put("destination",destination),null);
            }catch(Exception e){pendingExport=null;exportInProgress=false;target.delete();result(request,null,"无法打开导出窗口："+e.getMessage());}});
        }catch(Exception e){target.delete();runOnUiThread(()->{exportInProgress=false;result(request,null,"导出失败："+e.getMessage());});}});
    }
    @Override protected void onResume(){super.onResume();foreground=true;continuationHandler.post(this::continueInbound);worker.execute(()->{try{store.purgeExpired();stateEvent();}catch(Exception ignored){/* 到期行继续隐藏，下次进入回收站时接续清理并报告错误。 */}});}
    @Override protected void onPause(){foreground=false;super.onPause();}
    @Override protected void onStop(){if(camera!=null)camera.getCameraControl().enableTorch(false);torch=false;super.onStop();}
    @Override protected void onDestroy(){nextInbound.cancel();continuationHandler.removeCallbacksAndMessages(null);captureFlashGeneration++;captureFlashView.animate().cancel();stopCaptureVoice();stopCamera();if(phone!=null)phone.stop();worker.execute(()->store.close());worker.shutdown();unregisterReceiver(ocrUpdates);networkWorker.shutdown();if(voice!=null)voice.release();if(tone!=null)tone.release();web.removeJavascriptInterface("Native");web.destroy();super.onDestroy();}
}
