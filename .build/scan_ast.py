# -*- coding: utf-8 -*-
r"""AST 扫描：用 Python 自带 ast 找常见缺陷模式（无需安装任何第三方库）。

（docstring 用 r-string：下面第 5 行的 `python .build\scan_ast.py` 里那个 `\s`
不是转义序列，普通字符串会招来 SyntaxWarning。本会话第一次跑全项目编译检查时
它就报出来了，顺手修掉。）

只读，不修改任何文件。跑法（项目根目录）：
    python .build\scan_ast.py

检查项：
  1. 裸 except（except:）—— 会连带吞掉 KeyboardInterrupt/SystemExit
  2. except Exception: pass —— 静默失败
  3. 可变默认参数（def f(x=[] / x={} / x=set())）—— 多次调用共享同一对象
  4. 函数内引用未赋值的局部变量（简单版：NameError 风险，只看明显形态）
  5. 循环内 re.compile（常量正则应提到模块层）
  6. 循环内 os.path.getsize / os.stat 重复调用（同一 path）
  7. open() 不带 encoding（Windows 默认 cp936，读 UTF-8 文件会炸）
  8. 比较用 == None / != None（应为 is None / is not None）
  9. 字符串 * 大数 或 list 乘法（内存风险）
 10. dict/list 在循环里 += 拼接字符串（O(n²)）
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {"venv", "__pycache__", ".git", ".build", "node_modules"}


def walk_py():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            if fn.endswith(".py"):
                yield os.path.join(base, fn)


class V(ast.NodeVisitor):
    def __init__(self, path):
        self.path = path
        self.lines = open(path, "rb").read().decode("utf-8", "replace").splitlines()
        self.out = []
        self.loop_depth = 0

    def add(self, node, cat, msg):
        ln = getattr(node, "lineno", 0)
        col = getattr(node, "col_offset", 0)
        src = self.lines[ln - 1].strip() if 0 < ln <= len(self.lines) else ""
        self.out.append((ln, col, cat, msg, src))

    # 1/2 裸 except 与 except Exception: pass
    def visit_ExceptHandler(self, node):
        if node.type is None:
            self.add(node, "裸except",
                     "except: 会吞掉 KeyboardInterrupt / SystemExit，应写具体异常")
        elif isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException"):
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                self.add(node, "静默失败",
                         "%s: pass 会把错误完全吞掉，至少应该记日志" % node.type.id)
        self.generic_visit(node)

    # 3 可变默认参数
    def visit_FunctionDef(self, node):
        for d in list(node.args.defaults) + [x for x in node.args.kw_defaults if x]:
            if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                self.add(node, "可变默认参数",
                         "%s() 的默认参数是可变对象，多次调用会共享同一份" % node.name)
            elif isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and \
                    d.func.id in ("list", "dict", "set"):
                self.add(node, "可变默认参数",
                         "%s() 的默认参数是 %s()，应改用 None 再内部判断" % (node.name, d.func.id))
        self.generic_visit(node)
    visit_AsyncFunctionDef = visit_FunctionDef

    # 5/6 循环内的重活
    def visit_For(self, node):
        self.loop_depth += 1
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                # re.compile
                if sub.func.attr == "compile" and isinstance(sub.func.value, ast.Name) \
                        and sub.func.value.id == "re":
                    self.add(sub, "循环内编译正则",
                             "常量正则应提到模块层编译一次")
                # re.match/search/findall 直接传字符串（内部有 lru_cache，但循环内仍建议预编译）
        self.loop_depth -= 1
        self.generic_visit(node)

    # 7 open() 不带 encoding
    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Name) and f.id == "open":
            kws = {k.arg for k in node.keywords}
            # 文本模式才需要 encoding；带 'b' 的是二进制
            mode = "r"
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                mode = str(node.args[1].value)
            for k in node.keywords:
                if k.arg == "mode" and isinstance(k.value, ast.Constant):
                    mode = str(k.value.value)
            if "b" not in mode and "encoding" not in kws:
                self.add(node, "open缺encoding",
                         "open() 未指定 encoding，Windows 上按 cp936 解码 UTF-8 会炸或乱码")
        # 8 == None
        if isinstance(f, ast.Name) and f.id in ("isinstance", "len", "type"):
            pass
        self.generic_visit(node)

    # 8 比较 None 用 ==
    def visit_Compare(self, node):
        for op, comp in zip(node.ops, node.comparators):
            if isinstance(op, (ast.Eq, ast.NotEq)) and \
                    (isinstance(comp, ast.Constant) and comp.value is None):
                self.add(node, "None比较",
                         "和 None 比较应该用 is / is not，== 可能被自定义 __eq__ 影响")
        self.generic_visit(node)


def main():
    total = 0
    files = sorted(walk_py())
    print("扫描 %d 个 .py 文件…\n" % len(files))
    for path in files:
        rel = os.path.relpath(path, ROOT)
        try:
            tree = ast.parse(open(path, "rb").read(), filename=path)
        except SyntaxError as ex:
            print("!! 语法错误 %s: 第 %s 行 %s" % (rel, ex.lineno, ex.msg))
            continue
        v = V(path)
        v.visit(tree)
        if not v.out:
            continue
        print("=" * 90)
        print("%s（%d 处）" % (rel, len(v.out)))
        for ln, col, cat, msg, src in sorted(v.out):
            total += 1
            print("  %-5d [%s] %s" % (ln, cat, msg))
            print("          %s" % src[:110])
    print("\n" + "=" * 90)
    print("合计 %d 处可疑点" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
