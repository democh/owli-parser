"""文档转换适配器：LibreOffice（soffice）Office → PDF。

真实转换链路（二进制/字体/宏安全）需在安装 LibreOffice 的环境独立验收；
本目录的 contract suite 以 fake runner 锁定命令构造、错误映射与产物校验语义。
"""
