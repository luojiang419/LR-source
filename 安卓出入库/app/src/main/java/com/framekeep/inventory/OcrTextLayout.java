package com.framekeep.inventory;

import java.util.*;

/** 用文本框基线和邻近距离合并被拆成多个 block 的同一行。 */
final class OcrTextLayout {
    static final class Line {
        final String text;final int left,top,right,bottom;
        Line(String text,int left,int top,int right,int bottom){this.text=text;this.left=left;this.top=top;this.right=right;this.bottom=bottom;}
        int height(){return Math.max(1,bottom-top);}
    }
    static String text(List<Line> input){
        ArrayList<Line> sorted=new ArrayList<>(input);sorted.sort(Comparator.comparingInt((Line a)->a.top).thenComparingInt(a->a.left));
        ArrayList<ArrayList<Line>> lines=new ArrayList<>();
        for(Line item:sorted){
            ArrayList<Line> target=null;
            for(ArrayList<Line> group:lines){Line anchor=group.get(0);int overlap=Math.min(anchor.bottom,item.bottom)-Math.max(anchor.top,item.top);
                if(overlap>=Math.min(anchor.height(),item.height())*.65&&Math.abs((anchor.top+anchor.bottom)-(item.top+item.bottom))<=Math.max(anchor.height(),item.height())*.7){target=group;break;}}
            if(target==null){target=new ArrayList<>();lines.add(target);}target.add(item);
        }
        StringBuilder result=new StringBuilder();
        for(ArrayList<Line> group:lines){group.sort(Comparator.comparingInt(a->a.left));Line previous=null;
            for(Line item:group){
                if(previous!=null){int gap=item.left-previous.right;double character=Math.min((double)(previous.right-previous.left)/Math.max(1,previous.text.length()),(double)(item.right-item.left)/Math.max(1,item.text.length()));
                    // 距离过大按不同列隔开；极小字距恢复断开的字母数字，否则保留空格供候选判断。
                    result.append(gap>Math.max(previous.height(),item.height())*1.6?'\n':gap<=character*.65?'\0':' ');
                }else if(result.length()>0)result.append('\n');
                result.append(item.text);previous=item;
            }
        }
        return result.toString().replace("\0","");
    }
}
