rm assets/*.tmu
for i in `ls assets/*.mid`
do 
  python -m midi2tmu $i 
done
