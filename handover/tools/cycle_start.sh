#!/bin/zsh
# cycle_start.sh CYCLE -- snapshot the reference, start the shared servers, launch the five territories detached.
C=$1; D=/private/tmp/plexsim-cycle$C; H=$D/head$C
cd $D
set -a; . ./tokens.env; set +a
python3 $H/handover/tools/refcheck7.py $H/handover/tools/ref_cycle5_before.json > ref_before$C.txt; echo "refcheck rc=$?"
# shared servers: the frozen 32678 (C's f587, N's sim87), A's 32693, N's 32679, P's 32688/32689
python3 launch.py $H state/frozen32678.log env RD_TOKEN=dummy python3 server.py --port 32678 --catalog tests/catalog_ui.json --assert-claimed --gdm off --state $D/state/frozen.pickle
(cd A && python3 startsim.py)
(cd N && python3 start76.py 32679)
for p in 32688 32689; do python3 start.py $H $p tests/catalog_ui.json --state $D/P/state$p.pickle; done
sleep 5
for p in 32678 32693 32679 32688 32689; do printf "%s:%s " $p $(curl -s -o /dev/null -w '%{http_code}' -m3 localhost:$p/identity); done; echo
mkdir -p A/out A/i18n N/out
python3 launch.py C C/phase2_cycle$C.log sh -c 'sh phase2_76.sh && sh phase3_76.sh; echo C_ALL_DONE'
python3 launch.py B B/driver$C.log sh -c 'sh remote1_76.sh & sh localall76.sh & sh phase2_76.sh; wait; sh phase3_76.sh; echo B_ALL_DONE'
python3 launch.py A A/driver$C.log zsh $D/A/run76.sh
python3 launch.py N N/driver$C.log zsh $D/N/run77.sh
python3 launch.py P P/driver$C.log sh $D/P/run77.sh
echo launched
