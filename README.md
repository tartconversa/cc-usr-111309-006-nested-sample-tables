# 跨版本嵌套一致的仓库样本表

本项目保存虚构实验设施的仓湖源数据与Python读取工具。现有工具只加载文件并显示条数，不执行事实加工、解码、合并、联接或重映射。全部设备、样品、记录和数值均为合成。

## 确定性抽样基础设施

`src/sampling_protocol.py` 与 `src/sample_table.py` 实现 Bernoulli 式稳定哈希样本表生成器，只做可复现抽样，不运行任何统计预测模型，也不能由少量样本宣称统计代表性。

协议（编码版本 `canonical-key/v1`，与 `fixtures/sampling.json` 登记一致）：

- 每条原始记录有不可变 `sample_key`，按 `sample_key_fields` 顺序编码：NULL 为单字节 `N`；字符串为 `S` + uint32 大端字节长度 + UTF-8 字节；空串与 NULL 编码不同，绝不混同；非字符串/NULL 键字段直接拒绝。
- 哈希 `h = uint64_be(SHA256(seed_bytes || canonical_key)[:8])`。
- 比例为有理数 a/b，入选严格判定 `h*b < a*2^64`，全程整数运算，禁止浮点比较。
- 相同协议下小比例样本是大比例样本的严格子集；新增原始行不改变旧行入选结果；删除与内容更正反映在新版本快照中，内容更正不改变抽样身份；同键内容冲突拒绝，禁止以内容哈希代替 sample_key。
- 支持全局阈值与按预定 stratum 的分层阈值，阈值选择规则写入描述；Bernoulli 模式不保证每层恰好 k 条。
- bottom-k 模式单独标注 `append_stable=false` 并保存 `cutoff_hash`，不与 Bernoulli 模式混称。
- 样本目录版本化发布，旧版本保留可读；输出按 canonical_key 字节序固定排序，重跑相同输入字节一致。

## 字段与单位

sample_key_fields按配置顺序编码，NULL、空串和不同字段类型不混同；seed_hex是公开复现种子，不是安全密钥。比例用分子分母表示。

material-index.json记录全部输入内容哈希；fixtures保存原始资料与配置，计算输出可放.workspace-output，原始资料不应被覆盖。

## 运行已有读取器

Python 3.11以上，标准库即可，不需安装第三方依赖。

    python -m src.sample_reader fixtures/rows.jsonl
    python -m compileall -q src tests
    python -m unittest discover -s tests -v

处理外部下载数据时使用隔离模式，避免工作目录中的同名模块（如伪造的 json.py）被导入：

    python -I scripts/run_tests.py
    python -I scripts/golden_demo.py
    python -I src/sample_table.py global --rows fixtures/rows.jsonl \
        --key-fields site,id --seed-hex 111309 \
        --fraction 1/4 --fraction 1/2 --fraction 3/4 \
        --catalog .workspace-output/cli_global
    python -I src/sample_table.py stratified --rows fixtures/rows.jsonl \
        --key-fields site,id --seed-hex 111309 --stratum-field site \
        --stratum-fraction s1=1/2 --stratum-fraction s2=3/4 \
        --catalog .workspace-output/cli_stratified
    python -I src/sample_table.py bottom-k --rows fixtures/rows.jsonl \
        --key-fields site,id --seed-hex 111309 --k 2 \
        --catalog .workspace-output/cli_bottom_k

`src/sample_catalog_reader.py` 只加载已发布目录（含旧版本）并校验快照字节序，不重算抽样。

## 固定容器

    docker build -t nested-sample-tables:local .
    docker run --rm nested-sample-tables:local

容器镜像以完整digest固定，默认仅检查输入契约；实现新增分析功能所需依赖由开发者明确登记。
