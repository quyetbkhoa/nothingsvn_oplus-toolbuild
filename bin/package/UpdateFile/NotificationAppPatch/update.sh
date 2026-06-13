work_dir=$(pwd)
source $work_dir/functions.sh
MAIN_FOLDER="$work_dir/build/baserom/images"
repS="python3 $work_dir/bin/strRep.py"
APKEDITOR="java -jar $work_dir/bin/apktool/apke.jar"
region=$(cat $work_dir/bin/ddevice/rom_region.txt)
brand_os=$(cat $work_dir/bin/ddevice/brand_os.txt)
SYSTEM_EXT_FOLDER="$work_dir/build/baserom/images/system_ext"
ANDROID_VER=$(cat $work_dir/bin/ddevice/androidver.txt)

fix_noti_safe_boot_wl() {
    local package_name="$1"
    local xml_file="$2"

    if [[ -z "$package_name" || -z "$xml_file" || ! -f "$xml_file" ]]; then
        echo "Usage: fix_noti_safe_boot_wl <package_name> <xml_file>"
        return 1
    fi

    xmlstarlet ed -L \
        -a "(//filter-conf/p[@att])[1]" \
        -t elem -n "p" -v "" \
        -i "(//filter-conf/p[@att])[1]/following-sibling::p[1]" -t attr -n "att" -v "$package_name" \
        "$xml_file"
}


add_opt() {
    local value="$1"
    local tag="$2"
    local file="$3"

    if [[ -z "$value" || -z "$tag" || -z "$file" || ! -f "$file" ]]; then
        echo "Usage: add_opt <value> <tag> <file>"
        return 1
    fi

    # Insert new <tag>value</tag> AFTER the last <tag> of that type inside <filter-conf>
    xmlstarlet ed -L \
        -a "(//filter-conf/$tag)[last()]" \
        -t elem -n "$tag" -v "$value" \
        "$file"
}


add_sys_startup() {
    local level="$1"
    local type="$2"
    local pkg="$3"
    local file="$4"

    if [[ ! -f "$file" ]]; then
        echo "Error: File '$file' does not exist."
        return 1
    fi

    # Skip if already exists
    if xmlstarlet sel -t -m "//startup[@all='$level'][@type='$type'][@pkgName='$pkg']" -v "." -n "$file" | grep -q .; then
        echo "Entry for pkgName='$pkg' with all='$level' and type='$type' already exists."
        return 0
    fi

    # Add <startup ...></startup> (not self-closing)
    xmlstarlet ed -L \
        -s "/filter-conf" -t elem -n startup -v "" \
        -i "/filter-conf/startup[last()]" -t attr -n "all" -v "$level" \
        -i "/filter-conf/startup[last()]" -t attr -n "type" -v "$type" \
        -i "/filter-conf/startup[last()]" -t attr -n "pkgName" -v "$pkg" \
        "$file"

    echo "Added: <startup all=\"$level\" type=\"$type\" pkgName=\"$pkg\"></startup>"
}


add_idle() {
    local package_name="$1"
    local xml_file="$2"  

    if [[ -z "$package_name" ]]; then
        echo "Usage: add_idle <package_name>"
        return 1
    fi

    if [[ ! -f "$xml_file" ]]; then
        echo "Error: File '$xml_file' not found!"
        return 1
    fi

    xmlstarlet ed -L \
        -a "//filter-conf/wl[last()]" \
        -t elem -n wl -v "$package_name" \
        "$xml_file"
}

#Find stuff
IS_SAFEBOOT_WHITELIST=$(find "$MAIN_FOLDER" -type f -name "safe_boot_whitelist.xml" )
IS_SYS_ST=$(find "$MAIN_FOLDER" -type f -name "sys_startup_v3_config_list.xml")
IS_OPT=$(find "$MAIN_FOLDER" -type f -name "app_launch_opt_list.xml")
IS_IDLE=$(find "$SYSTEM_EXT_FOLDER" -type f -name "sys_deviceidle_whitelist.xml")


if [[ $region == "Domestic" && $ANDROID_VER == "15" ]]; then
GSYS15=$(find "$MAIN_FOLDER/system_ext" -type d -name "NotificationCenter")
GBLOB15="$work_dir/bin/package/UpdateFile/NotificationAppPatch/A15/NotificationCenter"
Athena=$(find "$MAIN_FOLDER/system_ext" -type d -name "Athena")
GAthena="$work_dir/bin/package/UpdateFile/NotificationAppPatch/A15/Athena"
rm -rf $GSYS15/*
mv $GBLOB15/* $GSYS15
echo "[MODS] - Fixing Banner And Lock Screen Notification"
rm -rf $Athena/*
mv $GAthena/* $Athena
echo "[MODS] - Done"
elif [[ $ANDROID_VER == "16" ]]; then
GSYS=$(find "$MAIN_FOLDER/system_ext" -type d -name "NotificationCenter")
GBLOB="$work_dir/bin/package/UpdateFile/NotificationAppPatch/A16/NotificationCenter"
Athena=$(find "$MAIN_FOLDER/system_ext" -type d -name "Athena")
GAthena="$work_dir/bin/package/UpdateFile/NotificationAppPatch/A16/Athena"
echo "[MODS] - Adding Global Blob To NotificationCenter"
rm -rf $GSYS/*
mv $GBLOB/* $GSYS
echo "[MODS] - Fixing Banner And Lock Screen Notification"
rm -rf $Athena/*
mv $GAthena/* $Athena
echo "[MODS] - Done"
else
echo "[WARN] - Not Support Region And Android Version!Skipping..."
fi

app_need_fix=("com.google.android.gm" "com.microsoft.office.outlook" "vnpay.smartacccount" "com.sacombank.ewallet" "com.vnpay.Agribank3g" "com.vietinbank.ipay" "com.bplus.vtpay"
"com.mbmobile" "com.vnpay.bidv" "com.beeasy.toppay" "vn.com.vng.zalopay" "com.tpb.mb.gprsandroid" "vn.com.msb.smartBanking" "xyz.be.cake" "com.ncb.bank" "com.vnpay.vpbankonline"
"io.lifestyle.plus" "com.ocb.liobank" "vn.com.techcombank.bb.app" "com.vib.myvib2" "tw.nekomimi.nekogram" "com.VCB" "com.mservice.momotransfer")
if [[ $region = "Domestic" ]];then
    echo "[PATCH] - Device region is DOMESTIC ==> Patching Notification"
    for app in "${app_need_fix[@]}"; do
        fix_noti_safe_boot_wl "$app" "$IS_SAFEBOOT_WHITELIST"
        add_opt "$app" "a" "$IS_OPT"
        add_opt "$app" "c" "$IS_OPT"
        add_sys_startup "1" "1" "$app" "$IS_SYS_ST"
        add_sys_startup "1" "2" "$app" "$IS_SYS_ST"
        add_idle "$app" "$IS_IDLE"
    done
else
    echo "[INFO] - Export ROM!No need to fix"
fi