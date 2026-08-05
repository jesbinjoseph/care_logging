#!/usr/bin/env python
"""Setup script for care_logging — split stdout/stderr logging plug for CARE."""

from setuptools import find_packages, setup

with open("README.md", encoding="utf-8") as readme_file:
    readme = readme_file.read()

setup(
    author="Open Health Care Network",
    author_email="support@ohc.network",
    python_requires=">=3.10",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Framework :: Django",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Natural Language :: English",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
    ],
    description="Care plug that routes ERROR+ logs to stderr and lower-severity logs to stdout",
    install_requires=[
        "django",
    ],
    license="MIT license",
    long_description=readme,
    long_description_content_type="text/markdown",
    include_package_data=True,
    keywords="care_logging care plug logging",
    name="care_logging",
    packages=find_packages(include=["care_logging", "care_logging.*"]),
    url="https://github.com/egovhealthcare/care_logging",
    version="0.1.0",
    zip_safe=False,
)
