package com.framekeep.inventory;
import org.junit.Test;
import static org.junit.Assert.*;
public class NextInboundCaptureTest {
    @Test public void waitsForSpeechCompletion(){NextInboundCapture n=new NextInboundCapture();long t=n.begin();assertFalse(n.take(t,true));n.audioFinished(t);assertTrue(n.take(t,true));}
    @Test public void completionOnlyContinuesOnce(){NextInboundCapture n=new NextInboundCapture();long t=n.begin();n.audioFinished(t);assertTrue(n.take(t,true));n.audioFinished(t);assertFalse(n.take(t,true));}
    @Test public void cancelInvalidatesAudioCallback(){NextInboundCapture n=new NextInboundCapture();long t=n.begin();n.cancel();n.audioFinished(t);assertFalse(n.take(t,true));}
    @Test public void backgroundWaitsUntilAllowed(){NextInboundCapture n=new NextInboundCapture();long t=n.begin();n.audioFinished(t);assertFalse(n.take(t,false));assertTrue(n.take(t,true));}
    @Test public void newRecordRejectsOldSpeechCallback(){NextInboundCapture n=new NextInboundCapture();long old=n.begin();long current=n.begin();n.audioFinished(old);assertFalse(n.take(current,true));n.audioFinished(current);assertTrue(n.take(current,true));assertFalse(n.take(old,true));}
}
