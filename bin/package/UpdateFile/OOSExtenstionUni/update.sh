work_dir=$(pwd)
source $work_dir/functions.sh
IMG="$work_dir/build/baserom/images"
MY_STOCK="$work_dir/build/baserom/images/my_stock"
BLOB="$work_dir/bin/package/UpdateFile/OOSExtenstionUni"
target=$(find "$IMG" -type f -name "build.prop")
model=$(cat $work_dir/bin/ddevice/device_model.txt)
region=$(cat $work_dir/bin/ddevice/rom_region.txt)
brand_os=$(cat $work_dir/bin/ddevice/brand_os.txt)
MAIN_FOLDER="$work_dir/build/baserom/images"
ANDROID_VER=$(cat $work_dir/bin/ddevice/androidver.txt)

#Adding Dialer to some region
if [[ $region == "Domestic" ]];then
  echo "[WARN] - Detect Domestic Region!!No need to do..."
elif [[ $region == "IN" ]]; then
  GContact=$(find "$MAIN_FOLDER" -type d -name "GoogleContacts")
  GMessage=$(find "$MAIN_FOLDER" -type d -name "Messages")
  GDialer=$(find "$MAIN_FOLDER" -type d -name "GoogleDialer")

  rm -rf $GContact
  rm -rf $GMessage
  rm -rf $GDialer

  echo "[MODS] - Adding OOS Dialer to India Region..."
  rm -rf $MY_STOCK/etc/config/app_v2.xml
  cp -rf $BLOB/APP_CFG/* $MY_STOCK/etc/config
  echo "[MODS] - Done"
  
elif [[ $region == "EUEX" ]]; then
  GContact=$(find "$MAIN_FOLDER" -type d -name "GoogleContacts")
  GDialer=$(find "$MAIN_FOLDER" -type d -name "GoogleDialer")

  rm -rf $GContact
  rm -rf $GDialer

  echo "[MODS] - Adding OOS Dialer to EU Region..."
  rm -rf $MY_STOCK/etc/config/app_v2.xml
  cp -rf $BLOB/APP_CFG/* $MY_STOCK/etc/config
  echo "[MODS] - Done"
  
elif [[ $region == "ROW" ]]; then
  GContact=$(find "$MAIN_FOLDER" -type d -name "GoogleContacts")
  GDialer=$(find "$MAIN_FOLDER" -type d -name "GoogleDialer")

  rm -rf $GContact
  rm -rf $GDialer

  echo "[MODS] - Adding OOS Dialer to Global Region..."
  rm -rf $MY_STOCK/etc/config/app_v2.xml
  cp -rf $BLOB/APP_CFG/* $MY_STOCK/etc/config
  echo "[MODS] - Done"
else
  GContact=$(find "$MAIN_FOLDER" -type d -name "GoogleContacts")
  GDialer=$(find "$MAIN_FOLDER" -type d -name "GoogleDialer")

  rm -rf $GContact
  rm -rf $GDialer

  echo "[MODS] - Adding OOS Dialer to Global Region..."
  rm -rf $MY_STOCK/etc/config/app_v2.xml
  cp -rf $BLOB/APP_CFG/* $MY_STOCK/etc/config
  echo "[MODS] - Done"
fi 

#Replace Theme From OOS TO COS
if [[ $brand_os == "OxygenOS" ]]; then
if [[ $ANDROID_VER == "15" ]]; then
  echo "[MODS] - Replace Icon..."
  rm -rf $MY_STOCK/media/theme/*
  cp -rf $BLOB/COS_THEME/A15/theme/* $MY_STOCK/media/theme
  echo "[MODS] - Done"
elif [[ $ANDROID_VER == "16" ]]; then
  echo "[MODS] - Replace Icon..."
  rm -rf $MY_STOCK/media/theme/*
  cp -rf $BLOB/COS_THEME/A16/theme/* $MY_STOCK/media/theme
  echo "[MODS] - Done"
fi

fi

if [[ $brand_os == "OxygenOS" ]]; then

#Fix Camera And Signal Issues For OnePlus 13T
if [[ $model == "CPH2723" ]]; then 
  if [[ $ANDROID_VER == "15" ]]; then
  echo "[MODS] - Fixing camera and signal for OnePlus 13T(A15)"
  cp -rf $BLOB/OP13T/A15/odm/* $IMG/odm
  cp -rf $BLOB/OP13T/A15/fw/* $IMG
  echo "[MODS] - Done"
  elif [[ $ANDROID_VER == "16" ]]; then
  echo "[MODS] - Fixing camera and signal for OnePlus 13T(A16)"
  cp -rf $BLOB/OP13T/A16/odm/* $IMG/odm
  cp -rf $BLOB/OP13T/A16/fw/* $IMG
  echo "[MODS] - Done"
  fi
  change_prop ro.vendor.oplus.market.name "OnePlus 13T"
fi

#Fix Camera And Signal Issues For OnePlus 13
if [[ $model == "CPH2649" || $model == "CPH2653" ]]; then
  if [[ $ANDROID_VER == "15" ]]; then
  echo "[MODS] - Fixing signal for OnePlus 13(A15)"
  cp -rf $BLOB/OP13/fw/A15/* $IMG
  echo "[MODS] - [MODS] - Done"
  elif [[ $ANDROID_VER == "16" ]]; then
  echo "[MODS] - Fixing signal for OnePlus 13(A16)"
  cp -rf $BLOB/OP13/fw/A16/* $IMG
  echo "[MODS] - [MODS] - Done"
  fi
fi

#Fix Camera And Signal Issues For OnePlus Ace 5
if [[ $model == "CPH2691" || $model == "CPH2645" ]]; then 
if [[ $ANDROID_VER == "15" ]]; then
  echo "[MODS] - Fixing Camera and signal for OnePlus Ace 5(A15)"
  cp -rf $BLOB/ACE5/A15/odm/* $IMG/odm
  cp -rf $BLOB/ACE5/A15/fw/* $IMG
  echo "[MODS] - Done"
elif [[ $ANDROID_VER == "16" ]]; then
  echo "[MODS] - Fixing Camera and signal for OnePlus Ace 5(A16)"
  cp -rf $BLOB/ACE5/A16/odm/* $IMG/odm
  cp -rf $BLOB/ACE5/A16/fw/* $IMG
  echo "[MODS] - Done"
fi
change_prop ro.vendor.oplus.market.name "OnePlus Ace 5"
fi

#Fix Camera And Signal Issues For OnePlus Ace 3
if [[ $model == "CPH2585" || $model == "CPH2609" ]]; then 
  echo "[MODS] - Fixing signal for OnePlus Ace 3"
  cp -rf $BLOB/ACE3/fw/* $IMG
  echo "[MODS] - Done"
  change_prop ro.vendor.oplus.market.name "OnePlus Ace 3"
fi

#No region check
sed -i 's/^ro.vendor.oplus.radio.sar_regionmark=.*/ro.vendor.oplus.radio.sar_regionmark=/' $target

sed -i "s/persist.sys.oplus.anim_level=2/persist.sys.oplus.anim_level=1/g" $MAIN_FOLDER/my_product/build.prop

echo "[MODS] - Disabling OTA Update..."
#Modify Feature
remove_feature oplus.software.startup_strategy_restrict
remove_feature com.oplus.ota.component_update_url
remove_feature com.oplus.ota.contributors_url
remove_feature com.oplus.ota.new_appointment_url
remove_feature com.oplus.ota.recruit_promote_url
remove_feature com.oplus.ota.recruit_type_url
remove_feature com.oplus.ota.pubkey
remove_feature com.oplus.ota.pubkey_ver
remove_feature com.oplus.ota.show_version_type
remove_feature com.oplus.ota.questionnaire_support
remove_feature com.android.launcher.DEFAULT_DRAWER_MODE
remove_feature com.android.settings.screen_physics_size_cm
remove_feature com.oplus.wallpapers.ai_camera_movement
echo "[MODS] - Done"

else

echo "[WARN] - No Support Domestic Region Patch!"

fi 