package com.framekeep.inventory;
import org.junit.Test;
import static org.junit.Assert.*;
import java.time.*;
public class ExportRangeTest {
    @Test public void inclusiveLocalDay(){ExportRange r=ExportRange.dates("2026-11-05","2026-11-05",ZoneId.of("Asia/Shanghai"));assertEquals("2026-11-04T16:00:00Z",r.from);assertEquals("2026-11-05T16:00:00Z",r.until);}
    @Test public void daylightSavingUsesCalendarBoundary(){ExportRange r=ExportRange.dates("2026-03-08","2026-03-08",ZoneId.of("America/New_York"));assertEquals(23,Duration.between(Instant.parse(r.from),Instant.parse(r.until)).toHours());}
    @Test public void allHasNoBounds(){assertEquals("",ExportRange.all().from);assertEquals("",ExportRange.all().until);}
    @Test public void midnightNanosecondsAndOffsets(){ExportRange r=ExportRange.dates("2026-11-05","2026-11-05",ZoneId.of("Asia/Shanghai"));assertFalse(r.contains("2026-11-04T15:59:59.999999999Z"));assertTrue(r.contains("2026-11-05T00:00:00+08:00"));assertTrue(r.contains("2026-11-05T15:59:59.999999999Z"));assertFalse(r.contains("2026-11-05T16:00:00Z"));}
    @Test(expected=IllegalArgumentException.class) public void reverseRangeRejected(){ExportRange.dates("2026-11-06","2026-11-05",ZoneId.of("UTC"));}
    @Test(expected=IllegalArgumentException.class) public void invalidDateRejected(){ExportRange.dates("2026-02-30","2026-03-01",ZoneId.of("UTC"));}
    @Test(expected=IllegalArgumentException.class) public void incompleteDateRejected(){ExportRange.dates("","2026-11-05",ZoneId.of("UTC"));}
}
