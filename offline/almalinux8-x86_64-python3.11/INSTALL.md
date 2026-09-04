# AlmaLinux 8 x86_64 Offline Installation

This bundle targets AlmaLinux 8.10 x86_64 with glibc 2.28. It installs Python
3.11.16 from the included CPython source and PySide6 6.8.3 from local wheels.

If you transferred `almalinux8-x86_64-python3.11.tar.gz`, place it under the
project's `offline/` directory and extract it from the project root:

```bash
tar -xzf offline/almalinux8-x86_64-python3.11.tar.gz -C offline
```

## 1. Prerequisites

Use the internal AlmaLinux repository to install the compiler and Python build
dependencies before disconnecting from the package repository:

```bash
sudo dnf groupinstall -y "Development Tools"
sudo dnf install -y zlib-devel bzip2-devel libffi-devel xz-devel readline-devel \
  sqlite-devel openssl-devel tk-devel make
```

## 2. Verify transferred files

From the project root:

```bash
sha256sum -c offline/almalinux8-x86_64-python3.11/SHA256SUMS
```

## 3. Build an isolated Python installation

```bash
cd offline/almalinux8-x86_64-python3.11/source
tar -xzf Python-3.11.16.tgz
cd Python-3.11.16
./configure --prefix=/opt/lto-python311 --with-ensurepip=install
make -j"$(nproc)"
sudo make altinstall
```

`altinstall` preserves AlmaLinux's system Python 3.6 and installs
`/opt/lto-python311/bin/python3.11`.

## 4. Create the application environment and install offline wheels

From the project root:

```bash
/opt/lto-python311/bin/python3.11 -m venv .venv
.venv/bin/python -m pip install --no-index \
  --find-links offline/almalinux8-x86_64-python3.11/wheels \
  -r requirements.txt
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

## 5. Start the application

```bash
PYTHONPATH=src .venv/bin/python -m tape_gui.main
```

The LTFS, IBM tape-driver, `rsync`, and `ltfs_ordered_copy` system packages are
not included in this Python bundle. Install them from the approved internal
repository before device commissioning.
