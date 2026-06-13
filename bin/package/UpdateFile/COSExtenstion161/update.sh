#!/bin/bash
# SPDX-License-Identifier: GPL-3.0

work_dir=$(pwd)
source $work_dir/functions.sh
source $work_dir/bin/ddevice/fetchINFO.sh
MAIN_FOLDER="$work_dir/build/baserom/images"
MY_STOCK="$work_dir/build/baserom/images/my_stock"
BLOB="$work_dir/bin/package/UpdateFile/COSExtenstion161"
BASE_REGION=$(cat $work_dir/bin/ddevice/rom_region.txt)
android=$(cat $work_dir/bin/ddevice/androidver.txt)

if [[ $ROMVERSION == "16.0.7" || $ROMVERSION == "16.0.8" ]]; then
sed -i "s/persist.sys.oplus.anim_level=2/persist.sys.oplus.anim_level=1/g" $MAIN_FOLDER/my_product/build.prop

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
remove_feature oplus.software.hans_restriction_exp
remove_feature oplus.software.hans_restriction
remove_feature oplus.software.pms_app_frozen
remove_feature oplus.software.intercept_lockscreen_service

if [[ $BASE_REGION == "Domestic" && $android == "16" ]]; then 
  remove_feature com.oplus.wallpapers.online_wallpaper.host
  remove_feature com.oplus.dcs.data.url
  remove_feature com.oplus.dcs.tech.url
  remove_feature com.oplus.dcs.eap.url
  remove_feature com.oplus.dcs.storage.url
  remove_feature com.oplus.romupdate.url
  remove_feature com.oplus.romupdate.pubkey
  remove_feature com.oplus.romupdate.pubkey_ver
  remove_feature com.oplusos.sau.url
  remove_feature com.oplusos.sau.pubkey
  remove_feature com.oplusos.sau.pubkey_ver
  remove_feature com.oplus.smartsidebar.scene_rules_url
  remove_feature com.oplusos.deepthinker.apptype.host
  remove_feature com.oplusos.deepthinker.apptype.cert_version
  remove_feature com.oplusos.deepthinker.apptype.public_key
  remove_feature com.oplus.deepthinker.domain_name_release
  remove_feature com.oplus.ai.support_smart_voice
  remove_feature com.oplus.aimemory.export_bucket
  remove_feature com.oplus.aiunit.tools.transmit
  remove_feature com.customer.feedback.sdk_data_region
  remove_feature com.oplus.aimemory.odin_allawntech_url
  remove_feature com.oplus.aimemory.kms_key
  remove_feature com.oplus.pantanal.ums.is_support_group_card
  remove_feature com.oplus.aiunit.plugin.cloud_entity_extraction_oversea
  remove_feature com.android.settings.ai_settings_search_config
  remove_feature com.oplus.note.cloud_url_host
  remove_feature com.oplus.note.speech_url_domain
  remove_feature com.oplus.smartanalysis.rule_server_host
  remove_feature com.oplus.pantanal.ums.nebula_base_url
  remove_feature com.heytap.ocsp.client.captcha_url
  remove_feature com.heytap.ocsp.client.host_url
  remove_feature com.heytap.ocsp.client.h5_url
  remove_feature com.oplus.note.ai_privacy_policy_url_domain
  remove_feature com.oplus.aiwriter.main_host_address
  remove_feature com.oplus.aimemory.asr_url
  remove_feature com.oplus.aimemory.asr_app_id
  remove_feature com.oplus.aimemory.asr_sec_id
  remove_feature com.oplus.aimemory.asr_sec_key
  remove_feature com.oplus.aimemory.asr_package_name
  remove_feature com.oplus.gleanerservice.flashnotes_asr_config_oversea
  remove_feature com.oplus.aod.support_app_host
  remove_feature com.oplus.aod.scene_info_enable
  remove_feature com.oplus.weather.service.weather_host
  remove_feature andes.oplus.documentsreader.ai_privacy_policy_url_domain
  remove_feature com.oplus.romupdate.is_log_print_limit
  remove_feature com.oplus.romupdate.is_exp
  remove_feature com.oplus.directservice.aitoolbox_enable
  cp -rf $BLOB/feature/* $MY_STOCK/etc/extension
fi

fi