package com.framekeep.inventory;

/** 只有当前成功提示完成且界面允许时才接续一次；退出或新操作会使旧回调失效。 */
final class NextInboundCapture {
    private long generation=0;
    private boolean pending=false,ready=false;
    long begin(){pending=true;ready=false;return ++generation;}
    void audioFinished(long token){if(pending&&token==generation)ready=true;}
    long token(){return generation;}
    boolean take(long token,boolean allowed){if(!allowed||!pending||!ready||token!=generation)return false;pending=false;ready=false;return true;}
    void cancel(){pending=false;ready=false;generation++;}
}
