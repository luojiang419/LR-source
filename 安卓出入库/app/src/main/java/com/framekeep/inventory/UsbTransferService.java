package com.framekeep.inventory;

import android.app.*;
import android.content.*;
import android.content.pm.ServiceInfo;
import android.os.*;
import androidx.core.app.ServiceCompat;
import java.util.UUID;

/** 仅经受保护的 ADB 桥接启动，传输监听回环地址，会话超时自动关闭。 */
public final class UsbTransferService extends Service {
    static String token="";static final int PORT=8793;static volatile long accessed=0;static volatile boolean running=false;
    InventoryStore store;PhoneServer server;String ownedToken="";final Handler timer=new Handler(Looper.getMainLooper());
    static synchronized Bundle connect(Context context){
        if(token.isEmpty())token=UUID.randomUUID().toString().replace("-","");accessed=SystemClock.elapsedRealtime();
        androidx.core.content.ContextCompat.startForegroundService(context,new Intent(context,UsbTransferService.class).putExtra("sessionToken",token));
        Bundle data=new Bundle();data.putString("token",token);data.putInt("port",PORT);return data;
    }
    static synchronized void disconnect(Context context){context.stopService(new Intent(context,UsbTransferService.class));token="";running=false;OcrWorker.notifyRecord(context,"");}
    @Override public int onStartCommand(Intent intent,int flags,int startId){
        ownedToken=intent==null?"":intent.getStringExtra("sessionToken");if(ownedToken==null||ownedToken.length()<20){stopSelf();return START_NOT_STICKY;}
        NotificationManager notifications=getSystemService(NotificationManager.class);notifications.createNotificationChannel(new NotificationChannel("inventory-usb","USB 库存同步",NotificationManager.IMPORTANCE_LOW));
        PendingIntent open=PendingIntent.getActivity(this,0,new Intent(this,MainActivity.class),PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        Notification notification=new Notification.Builder(this,"inventory-usb").setSmallIcon(android.R.drawable.stat_notify_sync).setContentTitle("USB 库存同步").setContentText("正在与电脑同步出入库记录").setContentIntent(open).setOngoing(true).build();
        ServiceCompat.startForeground(this,8793,notification,Build.VERSION.SDK_INT>=29?ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC:0);
        try{if(server!=null&&!server.token.equals(ownedToken)){server.stop();server=null;store.close();store=null;}if(server==null){store=new InventoryStore(this);server=new PhoneServer(store,ownedToken,"127.0.0.1",PORT,()->accessed=SystemClock.elapsedRealtime(),id->OcrWorker.notifyRecord(this,id));running=true;OcrWorker.notifyRecord(this,"");}}
        catch(Exception failed){stopSelf();return START_NOT_STICKY;}
        timer.removeCallbacksAndMessages(null);timer.postDelayed(new Runnable(){public void run(){if(SystemClock.elapsedRealtime()-accessed>120000)stopSelf();else timer.postDelayed(this,15000);}},15000);
        return START_NOT_STICKY;
    }
    @Override public void onTimeout(int startId,int type){stopSelf();}
    @Override public IBinder onBind(Intent intent){return null;}
    @Override public void onDestroy(){timer.removeCallbacksAndMessages(null);if(server!=null)server.stop();if(store!=null)store.close();synchronized(UsbTransferService.class){if(token.equals(ownedToken)){token="";running=false;}}OcrWorker.notifyRecord(this,"");super.onDestroy();}
}
