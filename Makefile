.PHONY: test lab down demo train experiments report
PY ?= python3
test:        ; $(PY) -m pytest -q
lab:         ; sudo bash lab/netns.sh up
down:        ; sudo bash lab/netns.sh down
train:       ; sudo -E $(PY) models/collect.py --out models/lab_flows.csv && $(PY) models/train.py --data models/lab_flows.csv
experiments: ; sudo -E $(PY) experiments/run.py --quick --assert
report:      ; $(PY) experiments/report.py
demo:        ; sudo -E $(PY) api/serve.py   # then open http://127.0.0.1:8000
