# 分布式系统面试重点

## 基础理论
- CAP定理：三者不可兼得，网络分区必然存在时的取舍。
- BASE理论：基本可用、软状态、最终一致性，与ACID的对比。
- 一致性模型：强一致、单调读、因果一致、最终一致。

## 共识算法
- Paxos：Basic Paxos角色（Proposer/Acceptor/Learner）与两阶段流程。
- Raft：Leader选举（任期+日志对比）、日志复制、安全性保证。
- ZAB：与Raft的异同，ZXID结构（epoch + counter）。

## 分布式锁
- Redis分布式锁：`SET NX EX` + Lua释放、Redisson可重入锁与看门狗。
- ZooKeeper分布式锁：临时顺序节点、Watcher机制、羊群效应避免。
- 选型：性能优先Redis，可靠性优先ZK。

## 分布式事务
- 2PC：准备→提交，同步阻塞与单点故障问题。
- TCC：Try/Confirm/Cancel，空回滚与幂等处理。
- SAGA：正向补偿 vs 反向补偿，适合长事务。
- Seata AT模式：全局锁 + 一阶段提交 + 二阶段回滚。

## 分布式ID
- 雪花算法（Snowflake）：64位结构、时钟回拨问题与解决。
- UUID、数据库自增、号段模式（Leaf）的优劣对比。

## API网关与RPC
- 网关核心职责：路由、鉴权、限流、熔断、可观测。
- HTTP vs RPC：协议开销、服务治理、跨语言、适用场景。
- Dubbo架构：Provider/Consumer/Registry/Monitor，负载均衡策略。

## 一致性哈希
- 虚拟节点解决数据倾斜，新增/移除节点时最小化数据迁移。
