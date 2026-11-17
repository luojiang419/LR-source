package com.framekeep.inventory;

import java.time.*;
import java.time.format.DateTimeParseException;

/** 日期以导出设备的本地日历为准，结束日期包含全天。 */
final class ExportRange {
    final String from,until;
    private ExportRange(String from,String until){this.from=from;this.until=until;}
    static ExportRange all(){return new ExportRange("","");}
    boolean contains(String timestamp){if(from.isEmpty())return true;Instant at=OffsetDateTime.parse(timestamp).toInstant();return !at.isBefore(Instant.parse(from))&&at.isBefore(Instant.parse(until));}
    static ExportRange dates(String start,String end,ZoneId zone){
        if(!start.matches("\\d{4}-\\d{2}-\\d{2}")||!end.matches("\\d{4}-\\d{2}-\\d{2}"))throw new IllegalArgumentException("请选择开始日期和结束日期");
        try {
            LocalDate first=LocalDate.parse(start),last=LocalDate.parse(end);
            if(last.isBefore(first))throw new IllegalArgumentException("结束日期不能早于开始日期");
            return new ExportRange(first.atStartOfDay(zone).toInstant().toString(),last.plusDays(1).atStartOfDay(zone).toInstant().toString());
        } catch(DateTimeParseException e){throw new IllegalArgumentException("请选择有效的日期",e);}
    }
}
