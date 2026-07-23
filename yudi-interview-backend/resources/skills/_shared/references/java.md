# Java面试重点（基础+集合+并发+JVM）

## 基础概念
- JVM/JDK/JRE区别，字节码与"编译+解释"执行模型，AOT vs JIT。
- 8种基本类型与包装类，自动装箱/拆箱与Integer Cache。
- `==` vs `equals()`，`hashCode()`与`equals()`一致性约束。
- 方法重载 vs 重写，静态分派与动态分派。
- 接口 vs 抽象类，Java 8+ default方法的影响。
- 深拷贝 vs 浅拷贝，序列化方案。

## String
- 不可变性原理（final byte[]），安全与性能影响。
- 字符串常量池：`intern()`、编译期优化、`new String("abc")`创建对象数。
- `String` vs `StringBuilder` vs `StringBuffer`。

## 集合框架
- List：ArrayList（动态数组、扩容1.5倍）vs LinkedList（双向链表），RandomAccess标记。
- Map：HashMap底层（数组+链表+红黑树）、负载因子与扩容、线程不安全场景。
- HashMap长度为何是2的幂次方，多线程死循环问题。
- ConcurrentHashMap：JDK 7分段锁 vs JDK 8 CAS+synchronized，key/value不为null。
- Queue：BlockingQueue接口，ArrayBlockingQueue vs LinkedBlockingQueue。

## 并发
- 线程生命周期与状态转换，上下文切换成本。
- 死锁：条件、检测（jstack/arthuras）、预防策略。
- JMM：可见性、有序性、happens-before；volatile保证可见性+禁止重排序但不保证原子性。
- synchronized底层原理（Monitor）、锁升级（偏向→轻量→重量）、偏向锁废弃。
- ReentrantLock vs synchronized：可中断、公平锁、Condition、超时获取。
- CAS与ABA问题，Atomic原理。
- 线程池：核心参数（corePoolSize/maxPoolSize/queue/handler）、拒绝策略、动态配置。
- CompletableFuture：编排、异常处理、自定义线程池。

## JVM
- 运行时数据区：堆/栈/方法区/元空间/程序计数器/直接内存。
- GC判断：引用计数 vs 可达性分析；四种引用（强/软/弱/虚）。
- GC算法：标记-清除、复制、标记-整理、分代收集。
- 垃圾收集器：Serial→Parallel→CMS→G1→ZGC，各自适用场景。
- 双亲委派模型与打破方式（SPI、OSGi、线程上下文类加载器）。
- OOM排查：Heap Dump、jmap/jstat/arthuras、GC日志分析。
