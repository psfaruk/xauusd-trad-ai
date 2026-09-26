#!/bin/bash
# D-078 — full backend suite (detached runner; results land in run/d078_suite.log)
cd /home/z/my-project/xauusd-trad-ai/backend
export PYTHONUNBUFFERED=1
python -m pytest tests/ -q > /home/z/my-project/run/d078_suite.log 2>&1
echo "EXIT=$?" >> /home/z/my-project/run/d078_suite.log
