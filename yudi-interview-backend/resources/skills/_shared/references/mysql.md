# MySQL面试重点

## 索引
- B+树为什么适合磁盘索引（有序、范围查询、叶子链表）。
- 覆盖索引与回表成本，联合索引最左前缀原则与索引下推。
- 索引失效场景：函数转换、隐式类型转换、OR、LIKE前缀通配、非最左列。
- EXPLAIN执行计划：type/key/Extra字段含义，Extra中Using filesort/Using temporary。

## 事务与MVCC
- ACID含义，事务隔离级别（RU/RC/RR/SERIALIZABLE）与各自解决的问题。
- MySQL默认RR，InnoDB通过MVCC + Next-Key Lock解决幻读。
- MVCC原理：隐藏列（trx_id/roll_pointer）、Undo Log版本链、ReadView。
- 当前读 vs 快照读，RR下当前读仍加间隙锁。

## 锁机制
- 表级锁 vs 行级锁，InnoDB行锁（Record/Gap/Next-Key）。
- 意向锁的作用（快速判断表级冲突），IS/IX与S/X的兼容矩阵。
- 死锁检测与避免：按固定顺序加锁、缩短事务、降低隔离级别。

## 存储引擎与日志
- InnoDB vs MyISAM：事务、行锁、外键、崩溃恢复。
- Redo Log（WAL、crash-safe）vs Undo Log（MVCC、回滚）vs Binlog（主从复制、归档）。
- 两阶段提交保证Redo Log与Binlog一致性。

## 性能优化
- 慢SQL定位：`slow_query_log`、pt-query-digest。
- 分库分表策略：垂直拆分 vs 水平拆分，ShardingSphere中间件。
- 深度分页优化：游标分页、延迟关联，子查询先查主键。
