package com.shiying.ocr

import android.content.Context
import android.graphics.Bitmap
import com.paddle.ocr.EngineConfig
import com.paddle.ocr.PaddleOCRConfig
import com.paddle.ocr.engine.OCREngine
import com.paddle.ocr.util.OpenCVUtils
import org.json.JSONArray
import org.json.JSONObject

/** 同步桥接只由后台线程调用；每次结束释放会话，避免大模型常驻挤占相机内存。 */
object PaddleEngine {
    @JvmStatic
    @Synchronized
    fun recognize(context: Context, bitmap: Bitmap, modelRoot: String, detector: String, recognizer: String): JSONObject {
        // 保留原始链接错误，便于真机定位 ABI / 系统符号兼容性。
        System.loadLibrary("opencv_java4")
        check(OpenCVUtils.init(context)) { "OpenCV 初始化失败" }
        org.opencv.core.Core.setNumThreads(2)
        val config = PaddleOCRConfig(detLimitSideLen = 1280, detLimitType = "max", detMaxSideLimit = 1600,
            recScoreThresh = 0.0f, recBatchSize = 1)
        val engine = OCREngine(context.applicationContext, config, EngineConfig(numThreads = 2),
            "$modelRoot/$detector/inference.onnx", "$modelRoot/$recognizer/inference.onnx", "$modelRoot/$recognizer/inference.yml")
        return try {
            val output = engine.run(bitmap)
            val lines = JSONArray()
            for (line in output.results) {
                val points = JSONArray()
                for (point in line.box.points) points.put(JSONArray().put(point.x.toDouble()).put(point.y.toDouble()))
                lines.put(JSONObject().put("text", line.text).put("confidence", line.confidence.toDouble()).put("points", points))
            }
            JSONObject().put("lines", lines).put("milliseconds", output.totalTimeMs)
                .put("loadMilliseconds", output.coldLoadTimeMs).put("detectionMilliseconds", output.detectionTimeMs)
                .put("recognitionMilliseconds", output.recognitionTimeMs).put("loadedPssKb", android.os.Debug.getPss())
        } finally { engine.release() }
    }
}
