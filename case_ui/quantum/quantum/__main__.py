"""Run the Meridian quantum assessment:  python -m quantum"""
from .case_meridian import result
from .waterfall import render_text

if __name__ == "__main__":
    print(render_text(result()))
