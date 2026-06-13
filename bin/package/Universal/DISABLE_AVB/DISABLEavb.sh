work_dir=$(pwd)
source $work_dir/functions.sh

disable_avb_verify $work_dir/build/baserom/images/vendor >/dev/null 2>&1
bash $work_dir/bin/package/DISABLE_AVB/HMATools/start

