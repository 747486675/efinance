"""Legacy build metadata for Python 3.6 / setuptools 59 (pre-PEP 621)."""
import re
from pathlib import Path

from setuptools import find_packages, setup

root = Path(__file__).parent
version = re.search(r'^version = "([^"]+)"', (root / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
setup(
    name="efinance", version=version, python_requires=">=3.6",
    description="A finance tool to get stock,fund and futures data base on eastmoney",
    long_description=(root / "README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    author="micro sheep", author_email="micro-sheep@outlook.com", license="MIT",
    url="https://github.com/Micro-sheep/efinance",
    packages=find_packages(include=["efinance", "efinance.*"]),
    package_data={"efinance": ["web/static/*"]},
    install_requires=(root / "requirements.txt").read_text().splitlines(),
    extras_require={
        "excel": ["openpyxl>=3.0"], "parquet": ["pyarrow>=3.0"],
        "web": ["fastapi>=0.115,<1; python_version >= '3.12'",
                "uvicorn>=0.30,<1; python_version >= '3.12'",
                "plotly>=5.24,<7; python_version >= '3.12'"],
    },
    entry_points={"console_scripts": ["efinance=efinance.cli:main"]},
)
