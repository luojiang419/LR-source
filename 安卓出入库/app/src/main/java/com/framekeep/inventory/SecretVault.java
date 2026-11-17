package com.framekeep.inventory;
import android.content.*;
import android.security.keystore.*;
import android.util.Base64;
import java.security.*;
import javax.crypto.*;
import javax.crypto.spec.GCMParameterSpec;
final class SecretVault {
    static String ocrKey(Context context) throws Exception {
        KeyStore ks=KeyStore.getInstance("AndroidKeyStore");ks.load(null);
        String alias="framekeep-ocr-v1";
        if(!ks.containsAlias(alias)){KeyGenerator g=KeyGenerator.getInstance("AES","AndroidKeyStore");g.init(new KeyGenParameterSpec.Builder(alias,KeyProperties.PURPOSE_ENCRYPT|KeyProperties.PURPOSE_DECRYPT).setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build());g.generateKey();}
        Key key=ks.getKey(alias,null);SharedPreferences p=context.getSharedPreferences("vault",0);String saved=p.getString("ocr",null);
        Cipher c=Cipher.getInstance("AES/GCM/NoPadding");
        if(saved==null){c.init(Cipher.ENCRYPT_MODE,key);byte[] ciphertext=c.doFinal(BuildConfig.OCR_KEY.getBytes(java.nio.charset.StandardCharsets.UTF_8));p.edit().putString("ocr",Base64.encodeToString(ciphertext,2)).putString("iv",Base64.encodeToString(c.getIV(),2)).commit();return BuildConfig.OCR_KEY;}
        c.init(Cipher.DECRYPT_MODE,key,new GCMParameterSpec(128,Base64.decode(p.getString("iv",""),2)));return new String(c.doFinal(Base64.decode(saved,2)),java.nio.charset.StandardCharsets.UTF_8);
    }
}
