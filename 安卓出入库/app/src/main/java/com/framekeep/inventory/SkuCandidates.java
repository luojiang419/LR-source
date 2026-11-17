package com.framekeep.inventory;

import java.text.Normalizer;
import java.util.*;
import java.util.regex.*;

/** 只在同一文本行内恢复 SKU，不跨行拼接不同字段。 */
final class SkuCandidates {
    static final class Analysis {
        final List<String> candidates,preferred;final boolean automatic;final String sku;
        Analysis(LinkedHashSet<String> all,LinkedHashSet<String> matches,boolean uncertain){
            preferred=new ArrayList<>(matches);candidates=new ArrayList<>(preferred);for(String s:all)if(!candidates.contains(s)&&candidates.size()<12)candidates.add(s);
            automatic=matches.size()==1&&!uncertain;sku=automatic?preferred.get(0):"";
        }
    }
    static Analysis analyze(String raw,String rules){
        String text=Normalizer.normalize(raw,Normalizer.Form.NFKC).toUpperCase(Locale.ROOT).replaceAll("[‐‑‒–—−]","-");
        List<String> prefixes=new ArrayList<>();for(String r:rules.toUpperCase(Locale.ROOT).split("[,，;；\\s]+"))if(!r.isEmpty())prefixes.add(r);
        LinkedHashSet<String> all=new LinkedHashSet<>(),preferred=new LinkedHashSet<>();boolean uncertain=false;
        for(String original:text.split("\\R")){
            String line=original.replaceAll("(?<=[A-Z0-9])[ \\t]*([._/-])[ \\t]*(?=[A-Z0-9])","$1");
            for(String prefix:prefixes){
                StringJoiner spaced=new StringJoiner("[ \\t]*");for(char letter:prefix.toCharArray())spaced.add(Pattern.quote(String.valueOf(letter)));
                line=line.replaceAll("(?<![A-Z0-9])"+spaced+"(?=[ \\t]*\\d)",Matcher.quoteReplacement(prefix));
            }
            // 前缀与数字间的空格通常来自模型的分词，不代表两个款号。
            line=line.replaceAll("(?<=[A-Z])[ \\t]+(?=\\d)","");
            for(String prefix:prefixes){
                Pattern digits=Pattern.compile("("+Pattern.quote(prefix)+"[A-Z0-9._/-]*\\d[A-Z0-9._/-]*)[ \\t]+(\\d[A-Z0-9._/-]*)(?=$|[^A-Z0-9._/-])");
                while(digits.matcher(line).find()){
                    uncertain=true;line=digits.matcher(line).replaceAll("$1$2");
                }
                Pattern suffix=Pattern.compile("("+Pattern.quote(prefix)+"[A-Z0-9._/-]*\\d[A-Z0-9._/-]*)[ \\t]+([A-Z]{1,2}\\d{1,8})(?=$|[^A-Z0-9])");
                Matcher joined=suffix.matcher(line);StringBuffer reconstructed=new StringBuffer();
                while(joined.find()){uncertain=true;joined.appendReplacement(reconstructed,Matcher.quoteReplacement(joined.group(1)+joined.group(2)));}
                joined.appendTail(reconstructed);line=reconstructed.toString();
            }
            Matcher m=Pattern.compile("[A-Z0-9]+(?:[._/-][A-Z0-9]+)*").matcher(line);
            while(m.find()){
                String candidate=m.group();if(candidate.length()>80||!candidate.matches(".*\\d.*"))continue;
                all.add(candidate);for(String prefix:prefixes)if(candidate.contains(prefix)){preferred.add(candidate);break;}
            }
        }
        return new Analysis(all,preferred,uncertain);
    }
}
