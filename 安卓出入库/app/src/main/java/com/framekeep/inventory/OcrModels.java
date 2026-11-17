package com.framekeep.inventory;

import java.util.*;

/** 设置、排队快照与端侧推理共用稳定标识。 */
final class OcrModels {
    static final String DEFAULT="paddle-small";
    static final List<String> ALL=Collections.unmodifiableList(Arrays.asList(DEFAULT,"paddle-tiny","paddle-medium","paddle-v5","mlkit-chinese","mlkit-latin"));
    static String require(String model){if(!ALL.contains(model))throw new IllegalArgumentException("OCR 模型无效");return model;}
    static String family(String model){
        switch(require(model)){
            case "paddle-small":return "PP-OCRv6_small";
            case "paddle-tiny":return "PP-OCRv6_tiny";
            case "paddle-medium":return "PP-OCRv6_medium";
            case "paddle-v5":return "PP-OCRv5_mobile";
            default:throw new IllegalArgumentException("请选择 PaddleOCR 模型");
        }
    }
}
