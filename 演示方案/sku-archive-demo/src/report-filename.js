export function archiveReportFilename(exportedAt = new Date()) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-GB', {
    timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',
    hour:'2-digit',minute:'2-digit',hourCycle:'h23',
  }).formatToParts(exportedAt).map(part => [part.type,part.value]));
  return `归档校验报表-${parts.year}${parts.month}${parts.day}${parts.hour}${parts.minute}.xlsx`;
}
