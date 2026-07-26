# input/ — 待處理區

放「一塊板子」的東西，跑 `python src/knowledge_extract` 生成 `data/<board>/`：

```
input/
├── <手冊>.pdf          # 官方手冊
└── <板子資料夾>/        # 整份 DTS 資料夾直接拖進來,兩種內部佈局都支援:
    ├── dts/*.dts …     #   a) archive/dts/<board> 的原樣(dts/ + include/)
    ├── include/…       #   b) .dts 直接放資料夾根層(+ include/)
    └── …
```

（也相容最舊的擺法：.dts/.dtsi 直接攤平在 input/ 底下。）

- 一次只放**一塊板**；多個 DTS 資料夾同時存在時只取字典序第一個並警告。
- 只有 DTS、沒有手冊時：`python src/knowledge_extract --steps dts --board <板名>`。
- 處理完建議把資料收進 `archive/` 留存，清空本資料夾。
