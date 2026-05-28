ZevaCode
==========

ZevaCode 是一款以現有等寬西文字型為基礎，再嵌入[宙黑體 ZevHeiTC-N](https://github.com/chiron-fonts/zev-hei-tc) CJK
字形的衍生字體，適合用於記事本、終端機/命令行、IDE 等環境。

專案目標是在原始等寬字型之上補上中文字元，不必設定 fallback。

## 特色

- 以等寬程式字型為基礎，保留原有的字形風格與 OpenType 功能。
- 嵌入[宙黑體 ZevHeiTC-N](https://github.com/chiron-fonts/zev-hei-tc) 的 CJK
  字形和其他全形符號。宙黑體是[昭源黑體](https://github.com/chiron-fonts/chiron-hei-hk)的改作，將原有的飾筆簡化，N
  版並移除「口」「山」一類部件底部的襯腳，使字形更簡潔、現代。
- 服務對象為繁體/正體中文使用者，但也包含簡體中文、日文、韓文等 CJK 字形。

## 下載

請前往 [Releases 頁面](https://github.com/chiron-fonts/ZevCode-TC/releases)下載最新版本的字型。

## 命名

字體名稱採用 `ZevaCode <變體> <家族代碼>` 的格式。

家族代碼對應上游來源字型，變體則區分標準版（Og）與「盡力而為」的寬度調整版（Al）。

以下會就字體的家族代碼和變體作一解説。為方便説明，雖然來源字型涵蓋拉丁字母、數字、標點符號等，而用作嵌入用的宙黑體除了中日韓字形還包括符號，以下仍以「英文字型」與「中文字型」來分別代指兩者。

## 變體

ZevaCode 的變體主要分別在於對 CJK 字形的寬度調整上。

| 代碼   | 說明        |
|------|-----------|
| `Al` | 寬度對齊版     |
| `Og` | 原中文字體寬度版本 |

`Al` 版在嵌入宙黑體字形時會做以下調整:

- 將字距調整至上游等寬字型的 2 倍寬度（以中文字形為標準）
- 按需要將原來字寬略為加寬，避免字與字之間空白太多
- 將韓文字形的字寬調整至與中文/日文字形相同

上述變更乃「盡力而為 (best effort)」。宙黑體源自昭源黑體/思源黑體，本身不是等寬字形。「對齊」調整以中文字形字寬為基準按比例放大，除韓文字形外，其他字圖如果跟中日字形的字寬不同，則不會再處理。

`Og` 版採用原本中文字體寬度，未經調整，字距與原字型相同。這意味着中文字形的字寬可能不是英文等寬字型的兩倍，導致在多行文本中可能出現對齊不齊的情況。此一版本會將韓文字形的字寬調整至與中文/日文字形相同。

## 來源字型

ZevaCode 以多款等寬英文字型為基礎，再嵌入中文字體部份字碼的字圖，每款來源字型都會對應一個家族代碼。

每款基礎字型的字形風格、OpenType 功能等特性各有不同，詳情請參閲該字型的官方説明。

### JetBrains Mono ([網頁](https://www.jetbrains.com/lp/mono/)) ([Github](https://github.com/JetBrains/JetBrainsMono/))

JetBrains 為開發者設計的等寬字型。

ZevaCode 衍生字型及其家族代碼：

| 原字體名稱             | ZevaCode 家族代碼 | 備註                                                      |
|-------------------|---------------|---------------------------------------------------------|
| JetBrains Mono    | `JetMono`     |                                                         |
| JetBrains Mono NL | `JetMonoNL`   | 屬於 JetBrains Mono 的 “no ligatures” 版本，只在 Static Font 提供 |

提供格式：

| &nbsp;        | &nbsp; | 下載 ZIP 檔案名稱                                                                          |
|---------------|--------|--------------------------------------------------------------------------------------| 
| Variable Font | 有      | `ZevaCode-JetMono-Variable.zip`                                                      |
| Static Font   | TTF    | 正常版: `ZevaCode-JetMono-Static_TTF.zip` <br>NL 版: `ZevaCode-JetMonoNL-Static_TTF.zip` |

### Cascadia Code ([Github](https://github.com/microsoft/cascadia-code/))

Microsoft 製作的等寬字型，是目前 Windows Terminal 和 Visual Studio 的預設字型。

ZevaCode 衍生字型及其家族代碼：

| 原字體名稱         | ZevaCode 家族代碼 | 備註                                  |
|---------------|---------------|-------------------------------------|
| Cascadia Code | `CasCode`     | 標準版                                 |
| Cascadia Mono | `CasMono`     | 即 Cascadia Code 的 “no ligatures” 版本 |

提供格式：

| &nbsp;        | &nbsp;    | 下載 ZIP 檔案名稱                                                                                                                                                                                                  |
|---------------|-----------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Variable Font | 有         | `ZevaCode-CasCode-Variable.zip` (包含 CasCode 與 CasMono 兩個家族)                                                                                                                                                  |
| Static Font   | OTF 及 TTF | CasCode: `ZevaCode-CasCodeNF-Static_(OTF/TTF).zip`, (NF 版) `ZevaCode-CasCodeNF-Static_(OTF/TTF).zip`  <br>CasMono: `ZevaCode-CasMono-Static_(OTF/TTF).zip`, (NF 版) `ZevaCode-CasMonoNF-Static_(OTF/TTF).zip` |

### Mona Sans Mono ([網頁](https://github.com/mona-sans)) ([Github](https://github.com/github/mona-sans/))

Github 製作，與 Mona Sans 搭配的等寬字型。

ZevaCode 衍生字型及其家族代碼：

| 原字體名稱          | ZevaCode 家族代碼 | 備註 |
|----------------|---------------|----|
| Mona Sans Mono | `Gima`        |    |

提供格式：

| 字型格式          | 狀況                            | 下載 ZIP 檔案名稱                                                                                                   |
|---------------|-------------------------------|---------------------------------------------------------------------------------------------------------------|
| Variable Font | 無                             |                                                                                                               |
| Static Font   | OTF 及 TTF，並有 SemiCondensed 寬度 | `ZevaCode-Gima-Static_(OTF/TTF).zip` <br>SemiCondensed 字寬: `ZevaCode-Gima-SemiCondensed-Static_(OTF/TTF).zip` | 

按：SemiCondensed 字寬幾乎已是中文字形的一半，因此只提供 Al 版。

### Monaspace ([網頁](https://monaspace.githubnext.com/)) ([Github](https://github.com/githubnext/monaspace))

由 Github 的 GitHub Next 團隊製作的等寬字型，原字體共有 Neon、Argon、Xenon、Radon 四種風格。

ZevaCode 衍生字型及其家族代碼：

| 字體名稱            | 家族代碼           | 備註                    |
|-----------------|----------------|-----------------------|
| Monaspace Argon | `GimaspaceArg` | Neo-grotesque sans 風格 |
| Monaspace Neon  | `GimaspaceNeo` | Humanist sans 風格      |

提供格式：

| 字型格式          | 狀況                   | 下載 ZIP 檔案名稱                                                                                                                                                                                                                             |
|---------------|----------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Variable Font | 有（中文字對齊僅於 100% 字寬有效） | `ZevaCode-Gimaspace-Variable.zip` (包含 GimaspaceArg 及 GimaspaceNeo)                                                                                                                                                                      |                                                             
| Static Font   | OTF 及 TTF（僅提供正常寬度版本） | GimaspaceArg: `ZevaCode-GimaspaceArg-Static_(OTF/TTF).zip`, (NF 版) `ZevaCode-GimaspaceArgNF-Static_(OTF/TTF).zip` <br>GimaspaceNeo: `ZevaCode-GimaspaceNeo-Static_(OTF/TTF).zip`, (NF 版) `ZevaCode-GimaspaceNeoNF-Static_(OTF/TTF).zip` |

## CJK 嵌入說明

ZevaCode 是以原始字型為基礎，將 ZevHei TC (N 版) 的 CJK 字形嵌入其中，而非相反，不會更改原始字型的特性（包括 OpenType 功能）。

來自 ZevHei TC 的字碼，請參閲 `assets/unicode_blocks.txt`。若一個字碼同時存在於原始字型與 ZevHei TC 中，則會優先使用原始字型的字形。

嵌入方式為純粹將 ZevHei TC 的字圖複製到原始字型對應的 Unicode 字碼，不會嵌入中文字體的 CCMP、GSUB、GPOS 等 OpenType 功能，KERN
也不會做額外調整。這一般不會影響中日韓字形的顯示，但也意味著在某些特定情況下（例如須靠兩個字符組成的合字）的顯示可能會出現異常，另外字體也不支援直排。

## 備註

以下是在 Windows 作業系統下的一些個人使用經驗。

- 即使已定義好 Named instances，一些應用程式似乎仍未能完全支援 Variable Font 的所有樣式。
- 雖然 `Og` 版中文字寬並不是拉丁字寬的雙倍，但一些程式仍會將中文字泊齊到兩倍寬的位置。個人懷疑系統是基於字體 OS/2 表的
  avgCharWidth 或類似資訊作此處理。
- 以 Cascadia Code 製作的 `ZevaCode CasCode` 家族，其 `.otf` 版顯示正常，但以 Monaspace 製作的
  `ZevaCode Gimaspace` 等字款的 `.otf` 版在 Windows 上會出現字距異常（無視中文字圖定義字寬，強制與英文字圖相同，造成中文字重疊的情況）。
  `.ttf` 版本則無此問題。由於
  `.ttf` 是直接由 `.otf` 轉換而來，因此目前懷疑是 Windows 的 OpenType 引擎問題。解決方法是使用 `.ttf` 版本。

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