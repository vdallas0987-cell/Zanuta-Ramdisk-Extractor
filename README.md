# Zanuta Ramdisk Extractor

Extrai e valida **restore ramdisks** e **firmware components** (iBSS, iBEC, DeviceTree, KernelCache, SEP) de IPSWs de dispositivos iPhone A12/A13.

---

## Funcionalidades

- **Extracao de ramdisk** — streams o restore ramdisk DMG do IPSW para disco com validacao estrutural
- **Componentes de firmware** — extrai iBSS, iBEC, DeviceTree, KernelCache e SEP
- **Validacao macOS** (apenas em macOS):
  - `hdiutil verify` — verificacao nativa da integridade do DMG
  - `img4tool --verify` — verificacao de assinatura IMG4 para iBSS, iBEC, DeviceTree, SEP
  - `hdiutil attach -nomount` — confirmacao de que o ramdisk e montavel
- **Verificacao de digest** — SHA-384 a partir do BuildManifest.plist
- **Metadata sidecar** — JSON com dispositivo, firmware, digest e validacoes
- **Interface grafica** — PySide6 com tabela de IPSWs, log colorido e progresso
- **Linha de comando** — processamento batch com `--dry-run`

---

## Requisitos

- Python >= 3.11
- PySide6 >= 6.0 (runtime)
- _Opcional (macOS):_ `hdiutil` (nativo), `img4tool` para verificacao de assinatura

---

## Instalacao

```bash
git clone <repo>
cd ramdisk_extractor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Uso

### Interface grafica

```bash
python app.py
```

### Linha de comando

```bash
python backend.py ~/Downloads/IPSWs -o ./ramdisks
python backend.py ~/Downloads/IPSWs --dry-run
```

---

## Build standalone

Gera um executavel unico (via PyInstaller) em `dist/`:

```bash
pip install -r requirements-dev.txt
python build.py
```

Resultado:

| Plataforma | Ficheiro |
|-----------|----------|
| Linux     | `dist/ZanutaRamdiskExtractor` |
| macOS     | `dist/ZanutaRamdiskExtractor.app` |
| Windows   | `dist/ZanutaRamdiskExtractor.exe` |

---

## Testes

```bash
python -m unittest discover -s tests -v
```

---

## Extrutura do projeto

```
ramdisk_extractor/
├── app.py              # GUI PySide6
├── backend.py          # Facade + entry point CLI
├── build.py            # Script PyInstaller
├── extractor.py        # Logica de extracao
├── models.py           # Modelos de dados
├── parser.py           # Parsing de IPSW / BuildManifest
├── scanner.py          # Descoberta de ficheiros IPSW
├── validator.py        # Validacao DMG e componentes
├── worker.py           # Thread de background para a GUI
├── tests/              # Suite de testes (131 tests)
├── resources/          # Icones (icns, ico, png)
└── pyproject.toml      # Metadados do projeto
```

---

## Licenca

MIT
