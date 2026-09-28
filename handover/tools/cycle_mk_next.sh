#!/bin/zsh
# mk_next.sh OLD NEW HEADREV -- build cycle NEW's driver tree from cycle OLD's (cycle 79 onwards).
# Copies SCRIPTS and the files they READ, never outputs; resolves symlinks (a link into a session
# scratchpad dies with it); recreates the directory tree; repoints every path; exports HEADREV.
set -e
O=/private/tmp/plexsim-cycle$1; N=/private/tmp/plexsim-cycle$2; REV=$3
[ -e $N ] && { echo "$N exists"; exit 1; }
mkdir -p $N
# 1. the input set cycle 78 was built from (mtime < its copy time), taken from the repaired cycle-79 tree
L=/private/tmp/plexsim-cycle79/.copylist
# 2. files the cycle-78 copy dropped, and cycle 79's repairs
cat $L - > $N/.copylist <<'EOF'
N/nreq.py
N/restart76.py
N/killleg76.py
N/killleg76b.py
N/killleg76c.py
P/poster.png
C/ax1/old72/hubkeys.txt
EOF
( cd $O && /usr/bin/find A/mo -type f ) >> $N/.copylist
rsync -aL --files-from=$N/.copylist $O/ $N/
( cd /private/tmp/plexsim-cycle78 && for d in A B C N P; do /usr/bin/find $d -type d; done ) | while read x; do mkdir -p $N/$x; done
cp $O/start.py $O/launch.py $O/tokscan.sh $N/
mkdir -p $N/state $N/reports $N/windows$2 $N/android/N/cap $N/android/N/out $N/android/scripts $N/live$2 $N/accept
cp $O/windows$1/{win_compare.py,win_specials.py,serve32720.sh} $N/windows$2/; cp $O/windows$1/error_inventory$1.py $N/windows$2/error_inventory$2.py
( cd $O/android && cp *.py *.sh resources.json catalog_android.json catalog_android_specials.json tokens.env $N/android/ && cp scripts/*.py $N/android/scripts/ )
cp $O/live$1/start_live.py $N/live$2/
cp $O/tokens.env $N/tokens.env; chmod 600 $N/tokens.env $N/android/tokens.env
# 3. repoint
grep -rlI -e "plexsim-cycle$1" -e "head$1" -e "windows$1" -e "live$1" $N --include='*.py' --include='*.sh' | while read f; do
  sed -i '' -e "s#plexsim-cycle$1#plexsim-cycle$2#g" -e "s#head$1#head$2#g" -e "s#windows$1#windows$2#g" -e "s#live$1#live$2#g" -e "s#cycle$1-#cycle$2-#g" -e "s#android$1#android$2#g" -e "s#native$1#native$2#g" "$f"; done
sed -i '' -e "s#wt$1#head$2#g" $N/live$2/start_live.py
# 4. the frozen head
mkdir $N/head$2 && git -C /Users/bensarmiento/debridmediamanager/plexsim archive $REV | tar -x -C $N/head$2
ln -s /Users/bensarmiento/debridmediamanager/plexsim/catalog.json $N/head$2/catalog.json
ln -s /Users/bensarmiento/debridmediamanager/plexsim/webclient $N/head$2/webclient
echo "built $N from $O at $REV: $(wc -l < $N/.copylist) files; plexapi $(python3 -c 'import plexapi;print(plexapi.VERSION)')"
