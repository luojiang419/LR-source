package com.framekeep.inventory;
import org.junit.Test;
import static org.junit.Assert.*;
import java.util.*;

public class SkuCandidatesTest {
    @Test public void keepsCompleteSkuIncludingSuffix(){assertEquals("LC7877529-P105",SkuCandidates.analyze("SIZE:S\nLC7877529-P105\n261020 G93", "LC").sku);}
    @Test public void normalizesFullWidthDashesAndSpaces(){assertEquals("LC7877529-P105",SkuCandidates.analyze("ＬＣ ７８７７５２９ － Ｐ １０５", "LC").sku);}
    @Test public void missingSeparatorSuggestsWholeStringButRequiresReview(){var a=SkuCandidates.analyze("LC7877529 P105","LC");assertFalse(a.automatic);assertEquals(List.of("LC7877529P105"),a.preferred);assertFalse(a.candidates.contains("P105"));}
    @Test public void digitFragmentsCannotSilentlySaveTruncatedSku(){var a=SkuCandidates.analyze("LC 7877 529 - P105","LC");assertEquals(List.of("LC7877529-P105"),a.preferred);assertFalse(a.automatic);assertFalse(a.candidates.contains("LC7877"));}
    @Test public void splitPrefixRejoinsOnTheSameLine(){assertEquals("LC7877529-P105",SkuCandidates.analyze("L C 7877529-P105","LC").sku);}
    @Test public void characterFragmentsProduceOneWholeCandidateForReview(){var a=SkuCandidates.analyze("L C 7 8 7 7 5 2 9 - P 1 0 5","LC");assertEquals(List.of("LC7877529-P105"),a.preferred);assertFalse(a.automatic);}
    @Test public void neverJoinsTwoDifferentRows(){var a=SkuCandidates.analyze("LC7877529\nP105","LC");assertEquals("LC7877529",a.sku);assertTrue(a.candidates.contains("P105"));assertFalse(a.candidates.contains("LC7877529P105"));}
    @Test public void multiplePreferredRemainSuspect(){var a=SkuCandidates.analyze("LC1234-P10\nLC5678-P20","LC");assertFalse(a.automatic);assertEquals(2,a.preferred.size());}
    @Test public void mismatchNeverAutoApproves(){var a=SkuCandidates.analyze("AB1234-P10\n123456","LC");assertFalse(a.automatic);assertTrue(a.preferred.isEmpty());assertTrue(a.candidates.contains("AB1234-P10"));}
    @Test public void brandAndSizeWordsAreNotAppended(){assertEquals("LC7877529-P105",SkuCandidates.analyze("LC7877529-P105 BLACK SIZE 160","LC").sku);}
    @Test public void supportsSeveralPrefixRulesAndDeduplicates(){var a=SkuCandidates.analyze("AB1234-P10\nAB1234-P10","LC，AB");assertTrue(a.automatic);assertEquals(1,a.preferred.size());}
    @Test public void blankOrNoDigitRequiresReview(){assertFalse(SkuCandidates.analyze("", "LC").automatic);assertFalse(SkuCandidates.analyze("LC COTTON", "LC").automatic);}
    @Test public void oversizedCandidatesCannotAutoSave(){assertFalse(SkuCandidates.analyze("LC"+"1".repeat(80),"LC").automatic);}
    @Test public void sameBaselineFragmentsCanRebuildWholeSku(){assertEquals("LC7877529-P105",OcrTextLayout.text(List.of(new OcrTextLayout.Line("LC7877529",0,0,90,20),new OcrTextLayout.Line("-",92,1,102,21),new OcrTextLayout.Line("P105",104,0,144,20))));}
    @Test public void differentColumnsAndRowsAreKeptApart(){String text=OcrTextLayout.text(List.of(new OcrTextLayout.Line("LC1234",0,0,60,20),new OcrTextLayout.Line("P105",200,0,240,20),new OcrTextLayout.Line("LC5678",0,30,60,50)));assertEquals("LC1234\nP105\nLC5678",text);}
}
