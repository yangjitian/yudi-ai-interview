# Redis面试重点

## 数据类型与场景
- 五种基础类型：String/Hash/List/Set/ZSet，各类型底层编码与适用场景。
- 特殊类型：Bitmap（活跃统计）、HyperLogLog（UV去重）、Stream（消息队列）。
- ZSet底层为什么用跳表而不是红黑树/B+树（范围查询、实现简单、内存灵活）。

## 持久化与线程模型
- RDB（fork + COW）vs AOF（写后日志、fsync策略），混合持久化。
- Redis 6.0前单线程模型（避免锁竞争、IO多路复用），6.0后多线程IO（命令执行仍单线程）。

## 生产问题
- 缓存穿透（布隆过滤器/空值缓存）、缓存击穿（互斥锁/永不过期）、缓存雪崩（随机过期/多级缓存）。
- 缓存与数据库一致性：延迟双删、Canal监听Binlog、最终一致性方案。

## 分布式锁
- `SET key value NX EX`基本实现，误删问题与Lua原子释放。
- Redisson可重入锁原理（Hash结构 + Lua脚本），看门狗续期机制。

## 性能优化
- Pipeline批量减少RTT，Lua脚本保证原子性。
- BigKey检测与拆分（redis-rdb-tools、UNLINK异步删除）。
- HotKey发现与本地缓存 + 热点分散。

## 集群
- 主从复制（全量 + 增量）、哨兵模式（故障转移、主观/客观下线）。
- Cluster模式：16384槽位、Gossip协议、重定向（MOVED/ASK）。
