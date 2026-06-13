work_dir=$(pwd)
source $work_dir/functions.sh
MAIN_FOLDER="$work_dir/build/baserom/images"
repS="python3 $work_dir/bin/strRep.py"
APKEDITOR="java -jar $work_dir/bin/apktool/apke.jar"
repS="python3 $work_dir/bin/strRep.py"
region=$(cat $work_dir/bin/ddevice/rom_region.txt)
brand_os=$(cat $work_dir/bin/ddevice/brand_os.txt)
android=$(cat $work_dir/bin/ddevice/androidver.txt)

echo "[MODS] - Modification Settings..."
#ready for patch
mkdir -p $work_dir/apk_temp
isSettingsDIR=$(find "$MAIN_FOLDER/system_ext" -type d -name "Settings")
isSettings=$(find "$MAIN_FOLDER/system_ext" -type f -name "Settings.apk")
$APKEDITOR d -t raw -f -no-dex-debug -i $isSettings -o $work_dir/apk_temp/isSettings.apk.out >/dev/null 2>&1

OTAF="$work_dir/bin/package/Universal/OTASettings/information.ini"
OTAW="$work_dir/bin/package/Universal/OTASettings/method.txt"
Smali1=$(find "$work_dir/apk_temp/isSettings.apk.out" -type f -name AboutDeviceOtaUpdatePreference.smali)

$repS $OTAF $Smali1

cat $OTAW >> $Smali1

#Finishing
Settings=$(basename $isSettings)
$APKEDITOR b -f -i $work_dir/apk_temp/isSettings.apk.out -o $work_dir/apk_temp/final/$Settings >/dev/null 2>&1

if [ -f "$work_dir/apk_temp/final/$Settings" ]; then
    echo "[MODS] - Rebuilding Settings.apk..."
    rm -rf $isSettingsDIR/oat
	rm -rf $isSettingsDIR/$Settings
    cp -rf $work_dir/apk_temp/final/$Settings $isSettingsDIR
    rm -rf $work_dir/apk_temp
    if [ -f $isSettingsDIR/Settings.apk ]; then
        echo "[MODS] - Done."
    else
        echo "[MODS] - Failed to create symlink for Settings."
    fi
else
    echo "[MODS] - Failed to create symlink for Settings."
fi