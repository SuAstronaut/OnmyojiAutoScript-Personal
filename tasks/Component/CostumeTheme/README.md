# 卷轴（主题）皮肤

卷轴就是庭院**最右下角那个展开按钮**，游戏里叫「主题」。OAS 里对应 `ThemeType` 枚举，
资产放在本目录下，由 `dev_tools/assets_extract.py` 生成为 `CostumeThemeAssets`。

> 为什么单独一个组件目录、而不是塞进 `tasks/Component/Costume/`：那里是庭院那一家
> （`main1` ~ `main14` → `CostumeAssets`）。卷轴、战斗主题、幕间、鲤鱼旗各有自己的
> 组件目录（`CostumeTheme` / `CostumeBattle` / `CostumeShikigami` / `CostumeCarpBanner`），
> 互不混用。

---

## 一套卷轴要采两张图

收起态和展开态长得不一样，两者都可能在画面上，所以都要采：

| 状态 | 长什么样 | 对应现默认卷轴的哪张图 |
|---|---|---|
| 收起态 | 卷轴没展开时（庭院默认状态） | `tasks/Restart/login/login_login_scrooll_close.png` |
| 展开态 | 点开卷轴之后 | `tasks/Restart/login/login_login_scrooll_open.png` |

运行时探测**任一张命中即算该套命中**。

---

## 如何添加一套卷轴

### 1. 建目录、采两张图

在 `./tasks/Component/CostumeTheme` 下新建一个文件夹（比如 `theme2`），里面放一个
`image.json`。**把两条规则预填好，不要写 `[]` 占位**——标注工具读的就是这个已有文件
（`module/server/tool.py` 的 `load_rule_file` 对不存在的路径直接 404），预填之后就只剩
「按实际截图调 roi 数值」，不必在工具里从零框选：

```json
[
  {
    "itemName": "theme_2_scroll_close",
    "imageName": "theme2_theme_2_scroll_close.png",
    "roiFront": "1185,637,44,72",
    "roiBack": "1129,604,151,116",
    "method": "Template matching",
    "threshold": 0.7,
    "description": "新语明霄关"
  },
  {
    "itemName": "theme_2_scroll_open",
    "imageName": "theme2_theme_2_scroll_open.png",
    "roiFront": "1189,627,44,72",
    "roiBack": "1129,604,151,116",
    "method": "Template matching",
    "threshold": 0.7,
    "description": "新语明霄开"
  }
]
```

上面两组 `roiFront` / `roiBack` 是照同目录上一套（theme1）抄的起点：卷轴在同一个 UI
位置，抄来的框通常够用；但**不同皮肤的图案尺寸可能不同**——theme1 的卷轴是 44×72，
默认套只有 28×39，所以截完图要按实际调。`threshold` 固定 0.7，`description` 写
「皮肤名 + 关/开」。

然后把某个配置里 `script.device.serial` 对应的游戏切到**这套卷轴皮肤**，启动服务：

```bash
./toolkit/python.exe server.py
```

浏览器打开标注工具 **`http://127.0.0.1:22288/tool/annotator`**（端口取 `config/deploy.yaml`
的 `WebuiPort`），把目标指到上面那个 `image.json`，上传**切到该卷轴皮肤之后**的游戏截图，
框出两张：

| itemName | 生成出来的属性 | 框什么 |
|---|---|---|
| `theme_2_scroll_close` | `I_THEME_2_SCROLL_CLOSE` | 卷轴收起态 |
| `theme_2_scroll_open` | `I_THEME_2_SCROLL_OPEN` | 卷轴展开态 |

**⚠️ itemName 直接决定生成出来的属性名**，规则是 `I_` + itemName 转大写（下划线与数字原样保留）。
必须跟第 3 步映射表里的 value 一字不差：

```
theme_2_scroll_close  ->  I_THEME_2_SCROLL_CLOSE   ✅ 对得上
theme2_scroll_close   ->  I_THEME2_SCROLL_CLOSE    ❌ 少个下划线，对不上，会静默失效
```

保存时标注工具会**自动**跑 `AssetsExtractor` 重新生成 `tasks/Component/CostumeTheme/assets.py`
（见 `module/server/tool.py`），不需要手动执行 `dev_tools.assets_extract`。

### 2. 加枚举项

打开 `./tasks/Component/Costume/config.py`，照现有条目加一行：

```python
COSTUME_THEME_2 = 'costume_theme_2'  # 游戏里的正式名
```

### 3. 加映射

打开 `./tasks/Component/Costume/costume_base.py` 的 `theme_costume_model`，照着填：

```python
theme_costume_model = {
    ThemeType.COSTUME_THEME_1: {
        (RestartAssets, 'I_LOGIN_SCROOLL_CLOSE'): 'I_THEME_1_SCROLL_CLOSE',
        (RestartAssets, 'I_LOGIN_SCROOLL_OPEN'): 'I_THEME_1_SCROLL_OPEN',
    },
    ThemeType.COSTUME_THEME_2: {          # ← 新增这一项
        (RestartAssets, 'I_LOGIN_SCROOLL_CLOSE'): 'I_THEME_2_SCROLL_CLOSE',
        (RestartAssets, 'I_LOGIN_SCROOLL_OPEN'): 'I_THEME_2_SCROLL_OPEN',
    },
}
```

解释一下：**key** 是「要被替换掉的默认卷轴资产」（`(资产类, 属性名)`），**value** 是
「你刚采出来的新资产」的属性名。运行时会把默认那两张图就地改写成新皮肤的两张图。

### 4. 加中文名

打开 `./assets/i18n/zh-CN.json`，加一行（不然 OASX 的下拉框里显示的是 key 原文）：

```json
"costume_theme_2": "游戏里的正式名",
```

### 5. 验证

```bash
./toolkit/python.exe -m pytest tests/tasks/test_costume_theme_layer.py
```

然后跑一次任务。日志里应该出现：

```
Costume theme detected costume_theme_default -> costume_theme_2, config updated
```

并且 `config/<实例>.json` 的 `costume_theme_type` 变成 `costume_theme_2`。

---

## 第一套卷轴不需要改代码

`COSTUME_THEME_1` 的枚举项、中文名占位、`theme_costume_model` 的两条映射**都已经登记好了**，
只差图。所以：

1. 把两张图按上表采进 `tasks/Component/CostumeTheme/theme1/image.json`
   （itemName 用 `theme_1_scroll_close` / `theme_1_scroll_open`）
2. 把 `assets/i18n/zh-CN.json` 里 `costume_theme_1` 的值从占位的「新卷轴皮肤」改成正式名
3. 重启 server

**不用动任何 `.py`。**

---

## 图片资产为什么不在仓库里

新卷轴皮肤的模板图必须在**实际游戏里切到那套皮肤之后**截图采集，无法凭空生成。
所以仓库只交付机制（枚举 + 映射表 + 套用/回切 + 运行时探测），图片由使用者按上面的流程补。
没采集时映射里的资产取不到，代码会打一条 warning 跳过，不影响其他皮肤：

```
Theme costume asset I_THEME_2_SCROLL_CLOSE not found, skip
```
