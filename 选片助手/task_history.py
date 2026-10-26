"""只清理任务列表的展示记录，保留结果、去重和拍摄对账证据。"""

INACTIVE_STATUSES = ('completed', 'matched', 'confirmed', 'ignored', 'cancelled',
                     'failed', 'stale', 'review', 'unmatched')


def clear_inactive(connection, table):
    # 明确列出已结束状态，未知状态同排队、运行中一样保留，避免影响执行。
    return clear(connection, table, INACTIVE_STATUSES)


def initialize(connection, table):
    if 'task_hidden' not in {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}:
        connection.execute(f'ALTER TABLE {table} ADD COLUMN task_hidden INTEGER NOT NULL DEFAULT 0')
    # 已清理任务重新执行、失效或待复核时自动回到列表。
    connection.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_task_reappear
        AFTER UPDATE OF status ON {table}
        WHEN OLD.task_hidden=1 AND NEW.status!=OLD.status
        BEGIN UPDATE {table} SET task_hidden=0 WHERE rowid=NEW.rowid; END''')


def clear(connection, table, statuses):
    marks = ','.join('?' for _ in statuses)
    return connection.execute(f'UPDATE {table} SET task_hidden=1 WHERE task_hidden=0 AND status IN ({marks})', statuses).rowcount
