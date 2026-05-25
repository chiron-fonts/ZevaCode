ZevCode TC
==========

ZevCode TC 是一款以現有等寬西文字型為基礎，再嵌入[宙黑體 ZevHeiTC-N](https://github.com/chiron-fonts/zev-hei-tc) CJK
字形的衍生字體，適合用於記事本、終端機/命令行、IDE 等環境。

專案目標是在原始等寬字型之上補上中文字元，毋須 Fallback 字型或額外設定即可達到中英混排效果。

## 字型特色

- 以等寬程式字型為基礎，保留原有的字形風格與 OpenType 功能。
- 嵌入[宙黑體 ZevHeiTC-N](https://github.com/chiron-fonts/zev-hei-tc)的 CJK
  字形和其他全形符號。宙黑體是[昭源黑體](https://github.com/chiron-fonts/chiron-hei-hk)的改作，其 N
  版將原有的飾筆簡化，並移除「口」「山」一類部件底部的襯腳，使字形更簡潔、現代。
- 服務對象為繁體/正體中文使用者，但也包含簡體中文、日文、韓文等 CJK 字形。

## 詳情

### 命名

字體名稱採用 `ZevCodeTC-<家族代碼>-<變體>` 的格式。

家族代碼對應上游來源字型，變體則區分標準版（S）與「盡力而為」的寬度調整版（B）。

以下會就字體的家族代碼和變體作一解説。為方便説明，雖然來源字型涵蓋拉丁字母、數字、標點符號等，而用作嵌入用的宙黑體除了中日韓字形還包括符號，以下仍以「英文字型」與「中文字型」來分別代指兩者。

### 變體

ZevCode TC 的變體主要分別在於對 CJK 字形的寬度調整上。

| 代碼  | 說明                     |
|-----|------------------------|
| `S` | 標準版，使用原來中文字體的 CJK 字體寬度 |
| `B` | 「盡力而為」版                |

所謂的「盡力而為 (best effort)」，指的是有限度地調整原來中文字形的寬度，使之等同英文等寬字型的兩倍寬度，從而在視覺上（尤其在多行情況下）達到對齊效果。

為此，B 版在嵌入宙黑體字形時會做以下調整:

- 將字距調整至上游等寬字型的 2 倍寬度（以中文字形為基準）
- 按需要將原來字寬略為加寬，避免字與字之間空白太多

而所謂「有限度」，其意思是：

- 宙黑體源自思源黑體。思源黑體的一些字形（例如韓文字形）本身就比中文/日文字形窄。以中文字形為基準加寬後，韓文字形字寬仍會小於中文/日文字形。

### 來源字型

ZevCode TC 以多款等寬英文字型為基礎，再嵌入中文字體部份字碼的字圖，每款來源字型都會對應一個家族代碼。

由於每款基礎字型的字形風格、OpenType 功能等特性各有不同，詳情請參閲該字型的官方説明。

#### JetBrains Mono

專案地址: https://www.jetbrains.com/lp/mono/, https://github.com/JetBrains/JetBrainsMono/

ZevCode 衍生字型及其家族代碼：

| 原字體名稱             | ZevCode 家族代碼 | 備註                                                      |
|-------------------|--------------|---------------------------------------------------------|
| JetBrains Mono    | JBM          |                                                         |
| JetBrains Mono NL | JBMNL        | 屬於 JetBrains Mono 的 “no ligatures” 版本，只在 Static Font 提供 |

提供格式：

| &nbsp;        | &nbsp; |
|---------------|--------|
| Variable Font | 有      |
| Static Font   | TTF 格式 |

#### Cascadia Code

| &nbsp;    | &nbsp;                                      |
|-----------|---------------------------------------------|
| Github 網址 | https://github.com/microsoft/cascadia-code/ |

ZevCode 衍生字型及其家族代碼：

| 原字體名稱         | ZevCode 家族代碼 | 備註                                  |
|---------------|--------------|-------------------------------------|
| Cascadia Code | CCD          | 標準版                                 |
| Cascadia Mono | CMD          | 即 Cascadia Code 的 “no ligatures” 版本 |

提供格式：

| &nbsp;        | &nbsp;    |
|---------------|-----------|
| Variable Font | 無         |
| Static Font   | OTF 及 TTF |

#### Mona Sans Mono

專案地址: https://github.com/github/mona-sans/

ZevCode 衍生字型及其家族代碼：

| 原字體名稱          | ZevCode 家族代碼 | 備註 |
|----------------|--------------|----|
| Mona Sans Mono | MSM          |    |

Static font 提供 OTF 與 TTF 兩種格式。

| 字型格式          | 狀況                                    |
|---------------|---------------------------------------|
| Variable Font | 無                                     |
| Static Font   | OTF 及 TTF，並有 SemiCondensed 寬度 |

按：SemiCondensed 字寬幾乎已是中文字形的一半，因此只提供 B 版。

#### Monaspace

專案地址: https://github.com/githubnext/monaspace, https://monaspace.githubnext.com/

ZevCode 衍生字型及其家族代碼：

| 字體名稱            | 家族代碼 | 備註                    |
|-----------------|------|-----------------------|
| Monaspace Argon | GMA  | Neo-grotesque sans 風格 |
| Monaspace Neon  | GMN  | Humanist sans 風格      |

Static font 提供 OTF 與 TTF 兩種格式。

| 字型格式          | 狀況        |
|---------------|-----------|
| Variable Font | 無         |
| Static Font   | OTF 及 TTF |

按：原字體有 SemiWide 和 Wide 寬度變體，但 ZevCode TC 僅提供正常寬度版本。

## CJK 嵌入說明

ZevCode TC 是以原始字型為基礎，將 ZevHei TC (N 版) 的 CJK 字形嵌入其中，而非相反，不會更改原始字型的特性（包括 OpenType 功能）。

來自 ZevHei TC 的字碼，請參閲 `assets/unicode_blocks.txt`。若一個字碼同時存在於原始字型與 ZevHei TC 中，則會優先使用原始字型的字形。

嵌入方式為純粹將 ZevHei TC 的字圖複製到原始字型對應的 Unicode 字碼，不會嵌入中文字體的 CCMP、GSUB、GPOS 等 OpenType 功能，KERN
也不會做額外調整。這一般不會影響中日韓字形的顯示，但也意味著在某些特定情況下（例如須靠兩個字符組成的合字）的顯示可能會出現異常。

## 發布內容

本倉庫通常會包含：

1. 可變字型 `.ttf`
2. 靜態字型家族壓縮包（例如 `Static_OTF.zip`、`Static_TTF.zip`）

如果你只想安裝單一字型，通常直接下載對應家族／變體即可；如果你偏好在支援 variable font 的環境使用較少檔案數，也可以選擇
variable 版本。

## 備註

以下是在 Windows 作業系統下的一些個人使用經驗。

- 即使已定義好 Named instances，一些應用程式似乎仍未能完全支援 Variable Font 的所有樣式。
- 雖然 S 版中文字寬並不是拉丁字寬的雙倍，但一些程式仍會將中文字泊齊到兩倍寬的位置。個人懷疑系統是基於字體 OS/2 表的
  avgCharWidth 或類似資訊作此處理。
- 根據 Cascadia Code 製作 ZevCodeTC-CCD 等家族的 `.otf` 版顯示正常。但根據 Monaspace 製作 ZevCodeTC-MSA 等家族，`.otf` 版在
  Windows 上會出現字距異常的問題（無視中文字圖定義字寬，強制與英文字圖相同，於是出現中文字重疊的情況）。`.ttf` 版本則無此問題。其實
  `.ttf` 是直接由 `.otf` 轉換而來，因此目前懷疑是 Windows 的 OpenType 引擎問題。

## 授權

本倉庫發佈之字型以 **[SIL Open Font License 1.1（OFL-1.1）](https://openfontlicense.org/)** 為授權基礎。  
你可以依 OFL 的條款使用、散布與修改這些字型。

## 感謝

感謝以下專案的開發者：

- [Cascadia Code](https://github.com/microsoft/cascadia-code/)
- [JetBrains Mono](https://www.jetbrains.com/lp/mono/)
- [Mona Sans Mono](https://github.com/github/mona-sans/)
- [Monaspace](https://github.com/githubnext/monaspace/)

## 捐款

假如喜歡這款字體，歡迎通過 [Paypal.me 捐助本人](https://www.paypal.com/paypalme/tamcyhk)，謝謝！