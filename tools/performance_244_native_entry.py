"""仅供原生桌面验收调用，不修改安装入口。"""
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
import verify_performance_244 as acceptance
args=type('Args',(),{})()
args.engine_root=Path(os.environ['FRAMEKEEP_ACCEPTANCE_ENGINE_ROOT'])
args.profile='native'
args.port=int(sys.argv[sys.argv.index('--port')+1])
acceptance.serve(args)
