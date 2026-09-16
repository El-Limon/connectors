# Dragonwilds server symbols — build-id `3b4ce30aed886594`

- binary: `<server>/RSDragonwilds/Binaries/Linux/RSDragonwildsServer-Linux-Shipping`
- `.sym`: `<server>/RSDragonwilds/Binaries/Linux/RSDragonwildsServer-Linux-Shipping.sym`
- first `PT_LOAD` p_vaddr (load base): `0x200000` — **vaddr = rva + load base**
- `.text`: addr `0x4ba2000` size `0x908bd8e`
- `.init`: addr `0xdc2dd90` size `0x26`
- `.fini`: addr `0xdc2ddb8` size `0xe`
- `.rodata`: addr `0x92ce00` size `0x2ca30a0`
- resolved **101/101** wanted names

| name | rva | vaddr | records | contiguous | signature |
|---|---|---|---|---|---|
| `_init` | `0xda2dd90` | `0xdc2dd90` | 1 | yes | `_init` |
| `_fini` | `0xda2ddb8` | `0xdc2ddb8` | 1 | yes | `_fini` |
| `_start` | `0x49a2000` | `0x4ba2000` | 1 | yes | `_start` |
| `UObject::ProcessEvent` | `0x4e3cf50` | `0x503cf50` | 125 | yes | `UObject::ProcessEvent(UFunction*, void*)` |
| `UObject::FindFunction` | `0x4e3ce90` | `0x503ce90` | 2 | yes | `UObject::FindFunction(FName) const` |
| `UStruct::FindPropertyByName` | `0x4dc1df0` | `0x4fc1df0` | 8 | yes | `UStruct::FindPropertyByName(FName) const` |
| `StaticFindObject` | `0x4e763d0` | `0x50763d0` | 31 | yes | `StaticFindObject(UClass*, UObject*, char16_t const*, bool)` |
| `GetObjectsOfClass` | `0x4e9f490` | `0x509f490` | 6 | yes | `GetObjectsOfClass(UClass const*, TArray<UObject*, TSizedDefaultAllocator<32> >&, bool, EObjectFlags, EInternalObjectFlags)` |
| `GetObjectsWithOuter` | `0x4e9d250` | `0x509d250` | 243 | yes | `GetObjectsWithOuter(UObjectBase const*, TArray<UObject*, TSizedDefaultAllocator<32> >&, bool, EObjectFlags, EInternalObjectFlags)` |
| `UObjectBaseUtility::GetPathName` | `0x4e71150` | `0x5071150` | 13 | yes | `UObjectBaseUtility::GetPathName(UObject const*) const` |
| `FName::ToString` | `0x4c05cc0` | `0x4e05cc0` | 13 | yes | `FName::ToString() const` |
| `FName::ToStringInto` | `0x4c05d50` | `0x4e05d50` | 56 | yes | `FName::ToString(FString&) const` |
| `FName::FName` | `0x4c03fb0` | `0x4e03fb0` | 14 | yes | `FName::FName(char16_t const*, EFindName)` |
| `FName::GetPlainNameString` | `0x4c05930` | `0x4e05930` | 34 | NO | `FName::GetPlainNameString(char16_t (&) [1024]) const` |
| `UGameEngine::Tick` | `0x7f0a480` | `0x810a480` | 117 | yes | `UGameEngine::Tick(float, bool)` |
| `UJgxGameEngine::Tick` | `0x9b2eab0` | `0x9d2eab0` | 29 | yes | `UJgxGameEngine::Tick(float, bool)` |
| `UGameplayStatics::GetGameMode` | `0x7f46ef0` | `0x8146ef0` | 5 | yes | `UGameplayStatics::GetGameMode(UObject const*)` |
| `UGameplayStatics::GetGameState` | `0x7f46fe0` | `0x8146fe0` | 5 | yes | `UGameplayStatics::GetGameState(UObject const*)` |
| `UGameplayStatics::GetPlayerController` | `0x7f47fe0` | `0x8147fe0` | 36 | yes | `UGameplayStatics::GetPlayerController(UObject const*, int)` |
| `AGameStateBase::GetServerWorldTimeSeconds` | `0x7f5b810` | `0x815b810` | 5 | yes | `AGameStateBase::GetServerWorldTimeSeconds() const` |
| `UEngine::Exec` | `0x8728a30` | `0x8928a30` | 72 | yes | `UEngine::Exec(UWorld*, char16_t const*, FOutputDevice&)` |
| `UKismetSystemLibrary::ExecuteConsoleCommand` | `0x8073f80` | `0x8273f80` | 17 | yes | `UKismetSystemLibrary::ExecuteConsoleCommand(UObject const*, FString const&, APlayerController*)` |
| `FOutputDeviceRedirector::Get` | `0x4b8d5c0` | `0x4d8d5c0` | 4 | yes | `FOutputDeviceRedirector::Get()` |
| `FOutputDeviceRedirector::AddOutputDevice` | `0x4b8d6a0` | `0x4d8d6a0` | 4 | yes | `FOutputDeviceRedirector::AddOutputDevice(FOutputDevice*)` |
| `RequestEngineExit` | `0x4b53e50` | `0x4d53e50` | 6 | yes | `RequestEngineExit(char16_t const*)` |
| `ADominionGameMode::PreLogin` | `0xa5e3d30` | `0xa7e3d30` | 194 | yes | `ADominionGameMode::PreLogin(FString const&, FString const&, FUniqueNetIdRepl const&, FString&)` |
| `ADominionGameMode::PostLogin` | `0xa5e48c0` | `0xa7e48c0` | 55 | yes | `ADominionGameMode::PostLogin(APlayerController*)` |
| `ADominionGameMode::PreLogout` | `0xa5e4a70` | `0xa7e4a70` | 35 | yes | `ADominionGameMode::PreLogout(APlayerController*)` |
| `ADominionGameMode::RequestSaveGame` | `0xa5e4bc0` | `0xa7e4bc0` | 15 | yes | `ADominionGameMode::RequestSaveGame()` |
| `ADominionGameMode::SaveGame` | `0xa5e3840` | `0xa7e3840` | 70 | yes | `ADominionGameMode::SaveGame()` |
| `ADominionGameMode::CanSave` | `0xa5e3bf0` | `0xa7e3bf0` | 35 | NO | `ADominionGameMode::CanSave(bool, bool&) const` |
| `AGameModeBase::Logout` | `0x7f3a480` | `0x813a480` | 29 | yes | `AGameModeBase::Logout(AController*)` |
| `AGameMode::Logout` | `0x7f26ed0` | `0x8126ed0` | 17 | yes | `AGameMode::Logout(AController*)` |
| `ADominionGameSession::KickPlayer` | `0xa362450` | `0xa562450` | 67 | yes | `ADominionGameSession::KickPlayer(APlayerController*, FText const&)` |
| `ADominionGameSession::BanPlayer` | `0xa362740` | `0xa562740` | 67 | yes | `ADominionGameSession::BanPlayer(APlayerController*, FText const&)` |
| `ADominionGameSession::RemoveBanPlayer` | `0xa3632b0` | `0xa5632b0` | 88 | yes | `ADominionGameSession::RemoveBanPlayer(TSharedRef<FOnlineUser const, (ESPMode)1>)` |
| `ADominionGameSession::RemoveKickPlayer` | `0xa362fb0` | `0xa562fb0` | 88 | yes | `ADominionGameSession::RemoveKickPlayer(TSharedRef<FOnlineUser const, (ESPMode)1>)` |
| `UDedicatedServerSettings::GetBannedUsers` | `0xa351dd0` | `0xa551dd0` | 38 | yes | `UDedicatedServerSettings::GetBannedUsers(TArray<FServerKnownPlayer, TSizedDefaultAllocator<32> >&) const` |
| `UDedicatedServerSettings::SetBannedUsers` | `0xa351f90` | `0xa551f90` | 42 | yes | `UDedicatedServerSettings::SetBannedUsers(TArray<FDediServerBannedUser, TSizedDefaultAllocator<32> > const&)` |
| `UDedicatedServerSettings::PerformConfigSave` | `0xa351770` | `0xa551770` | 5 | yes | `UDedicatedServerSettings::PerformConfigSave()` |
| `UDedicatedServerSettings::TryGetKnownPlayer` | `0xa352170` | `0xa552170` | 31 | yes | `UDedicatedServerSettings::TryGetKnownPlayer(FUniqueNetIdRepl const&, FServerKnownPlayer&) const` |
| `UPlayerChatComponent::Server_SendChatMessage` | `0x9e2eaf0` | `0xa02eaf0` | 29 | yes | `UPlayerChatComponent::Server_SendChatMessage(FChatMessageData const&, FChatPlayerFilterData const&)` |
| `UPlayerChatComponent::execServer_SendChatMessage` | `0x9e2eea0` | `0xa02eea0` | 56 | yes | `UPlayerChatComponent::execServer_SendChatMessage(UObject*, FFrame&, void*)` |
| `UPlayerChatComponent::Client_ReceiveChatMessage` | `0x9e2e470` | `0xa02e470` | 40 | yes | `UPlayerChatComponent::Client_ReceiveChatMessage(FChatMessageData const&)` |
| `UPlayerChatComponent::Client_ReceivePlayerEvent` | `0x9e2e7b0` | `0xa02e7b0` | 27 | yes | `UPlayerChatComponent::Client_ReceivePlayerEvent(FChatPlayerEventData const&)` |
| `UDominionRuntimeBlueprintLibrary::TryGiveItemToPlayer` | `0xaa1a830` | `0xac1a830` | 20 | yes | `UDominionRuntimeBlueprintLibrary::TryGiveItemToPlayer(APlayerController const*, UItemData const*, int)` |
| `UInventoryComponent::GetAllItems` | `0xa611600` | `0xa811600` | 12 | yes | `UInventoryComponent::GetAllItems(TArray<UItem*, TSizedDefaultAllocator<32> >&) const` |
| `UInventoryComponent::GetNumItems` | `0xa608a50` | `0xa808a50` | 6 | yes | `UInventoryComponent::GetNumItems() const` |
| `UInventoryComponent::GetItemFromSlot` | `0xa60d620` | `0xa80d620` | 5 | yes | `UInventoryComponent::GetItemFromSlot(int) const` |
| `AActor::TeleportTo` | `0x7a91d50` | `0x7c91d50` | 94 | yes | `AActor::TeleportTo(UE::Math::TVector<double> const&, UE::Math::TRotator<double> const&, bool, bool)` |
| `UTeleportationSubsystem::GetTargetLocationNames` | `0xa7127e0` | `0xa9127e0` | 13 | yes | `UTeleportationSubsystem::GetTargetLocationNames() const` |
| `UTeleportationSubsystem::GetTargetLocationTransform` | `0xa70b250` | `0xa90b250` | 36 | yes | `UTeleportationSubsystem::GetTargetLocationTransform(FName const&, UE::Math::TTransform<double>&)` |
| `UTeleportationSubsystem::TeleportWithParams` | `0xa70b490` | `0xa90b490` | 158 | yes | `UTeleportationSubsystem::TeleportWithParams(ADominionPlayerCharacter*, FPlayerTeleportParams&, FTeleportToNamedLocationResult)` |
| `UDominionAISubsystem::OnAIKilled` | `0xa211730` | `0xa411730` | 15 | yes | `UDominionAISubsystem::OnAIKilled(ADominionAICharacter*, AActor*, FDominionDamageEvent const&)` |
| `UProgressComponent::OnAIKilled` | `0xa4bf4a0` | `0xa6bf4a0` | 68 | yes | `UProgressComponent::OnAIKilled(ADominionAICharacter*, AActor*, FDominionDamageEvent const&)` |
| `ADominionPlayerCharacter::Client_SendDeathEventTelemetry` | `0xa162cd0` | `0xa362cd0` | 35 | yes | `ADominionPlayerCharacter::Client_SendDeathEventTelemetry(FDominionDamageEvent const&) const` |
| `FUniqueNetIdEOS::ToString` | `0x9acb0b0` | `0x9ccb0b0` | 17 | yes | `FUniqueNetIdEOS::ToString() const` |
| `APlayerController::EnableCheats` | `0x834abb0` | `0x854abb0` | 1 | yes | `APlayerController::EnableCheats()` |
| `APlayerController::ConsoleCommand` | `0x8348f50` | `0x8548f50` | 8 | yes | `APlayerController::ConsoleCommand(FString const&, bool)` |
| `FMemory::Malloc` | `0x4a5d420` | `0x4c5d420` | 6 | yes | `FMemory::Malloc(unsigned long, unsigned int)` |
| `FMemory::Free` | `0x4a5d480` | `0x4c5d480` | 8 | yes | `FMemory::Free(void*)` |
| `StaticFindObjectPath` | `0x4e76db0` | `0x5076db0` | 14 | yes | `StaticFindObject(UClass*, FTopLevelAssetPath, bool)` |
| `UObject::StaticClass` | `0x4c6ce70` | `0x4e6ce70` | 1 | yes | `UObject::StaticClass()` |
| `UClass::StaticClass` | `0x4c6d5f0` | `0x4e6d5f0` | 1 | yes | `UClass::StaticClass()` |
| `UWorld::StaticClass` | `0x7a16760` | `0x7c16760` | 1 | yes | `UWorld::StaticClass()` |
| `UPlayerChatComponent::StaticClass` | `0x9e2f3e0` | `0xa02f3e0` | 6 | yes | `UPlayerChatComponent::StaticClass()` |
| `UItemData::StaticClass` | `0x9ee4c40` | `0xa0e4c40` | 1 | yes | `UItemData::StaticClass()` |
| `APlayerController::StaticClass` | `0x7c94c90` | `0x7e94c90` | 1 | yes | `APlayerController::StaticClass()` |
| `ADominionGameMode::StaticClass` | `0x9f31ae0` | `0xa131ae0` | 6 | yes | `ADominionGameMode::StaticClass()` |
| `ADominionPlayerState::StaticClass` | `0x9f52330` | `0xa152330` | 6 | yes | `ADominionPlayerState::StaticClass()` |
| `UTeleportationSubsystem::StaticClass` | `0xa11ab70` | `0xa31ab70` | 6 | yes | `UTeleportationSubsystem::StaticClass()` |
| `FApp::GetBuildVersion` | `0x4b145c0` | `0x4d145c0` | 1 | yes | `FApp::GetBuildVersion()` |
| `FEngineVersion::Current` | `0x4b78da0` | `0x4d78da0` | 9 | yes | `FEngineVersion::Current()` |
| `FEngineVersion::ToString` | `0x4b78c40` | `0x4d78c40` | 37 | yes | `FEngineVersion::ToString(EVersionComponent) const` |
| `UGameplayStatics::GetGameInstance` | `0x7f46e00` | `0x8146e00` | 5 | yes | `UGameplayStatics::GetGameInstance(UObject const*)` |
| `ADominionPlayerController::GetPlayerChatComponent` | `0xa555670` | `0xa755670` | 2 | yes | `ADominionPlayerController::GetPlayerChatComponent() const` |
| `ADominionPlayerController::ConstructPlayerChatSenderData` | `0xa540050` | `0xa740050` | 90 | yes | `ADominionPlayerController::ConstructPlayerChatSenderData(FChatPlayerSenderData&) const` |
| `FText::FromString` | `0x4aa3b60` | `0x4ca3b60` | 46 | yes | `FText::FromString(FString const&)` |
| `FTextInspector::GetDisplayString` | `0x4a9ea80` | `0x4c9ea80` | 7 | yes | `FTextInspector::GetDisplayString(FText const&)` |
| `FUniqueNetIdEOS::ParseFromString` | `0x9ac8140` | `0x9cc8140` | 88 | yes | `FUniqueNetIdEOS::ParseFromString(FString const&)` |
| `UDedicatedServerSettings::SynchronizeBannedListToKnownPlayers` | `0xa351ac0` | `0xa551ac0` | 72 | yes | `UDedicatedServerSettings::SynchronizeBannedListToKnownPlayers()` |
| `FOutputDeviceFile::Serialize` | `0x4b8bef0` | `0x4d8bef0` | 32 | yes | `FOutputDeviceFile::Serialize(char16_t const*, ELogVerbosity::Type, FName const&, double)` |
| `ADominionGameStateBase::StaticClass` | `0x9f379b0` | `0xa1379b0` | 6 | yes | `ADominionGameStateBase::StaticClass()` |
| `ADominionPlayerController::StaticClass` | `0xa16fe10` | `0xa36fe10` | 6 | yes | `ADominionPlayerController::StaticClass()` |
| `AWorldLodestone::StaticClass` | `0xa14a610` | `0xa34a610` | 6 | yes | `AWorldLodestone::StaticClass()` |
| `ADominionAICharacter::StaticClass` | `0x9f30ed0` | `0xa130ed0` | 1 | yes | `ADominionAICharacter::StaticClass()` |
| `UDominionRuntimeBlueprintLibrary::StaticClass` | `0xa175ed0` | `0xa375ed0` | 6 | yes | `UDominionRuntimeBlueprintLibrary::StaticClass()` |
| `AGameModeBase::PreLogin` | `0x7f38b30` | `0x8138b30` | 34 | yes | `AGameModeBase::PreLogin(FString const&, FString const&, FUniqueNetIdRepl const&, FString&)` |
| `ADominionPlayerController::OnNetCleanup` | `0xa54c750` | `0xa74c750` | 16 | yes | `ADominionPlayerController::OnNetCleanup(UNetConnection*)` |
| `APlayerController::OnNetCleanup` | `0x834b9b0` | `0x854b9b0` | 14 | yes | `APlayerController::OnNetCleanup(UNetConnection*)` |
| `ADominionPlayerController::Destroyed` | `0xa545630` | `0xa745630` | 12 | yes | `ADominionPlayerController::Destroyed()` |
| `AGameSession::NotifyLogout` | `0x7f59240` | `0x8159240` | 1 | yes | `AGameSession::NotifyLogout(APlayerController const*)` |
| `ADominionPlayerState::GetCharacterDisplayName` | `0xa367920` | `0xa567920` | 7 | yes | `ADominionPlayerState::GetCharacterDisplayName() const` |
| `UDisplayNameComponent::GetCharacterDisplayName` | `0xa32a410` | `0xa52a410` | 13 | yes | `UDisplayNameComponent::GetCharacterDisplayName(FDomOwnerGuid const&, FString const&) const` |
| `UDisplayNameComponent::IsCharacterNameReady` | `0xa32a3a0` | `0xa52a3a0` | 8 | yes | `UDisplayNameComponent::IsCharacterNameReady(FDomOwnerGuid const&) const` |
| `UAssetRegistryImpl::GetAssetsByClass` | `0x67ac800` | `0x69ac800` | 28 | yes | `UAssetRegistryImpl::GetAssetsByClass(FTopLevelAssetPath, TArray<FAssetData, TSizedDefaultAllocator<32> >&, bool) const` |
| `UGameplayStatics::ApplyDamage` | `0x7f3d040` | `0x813d040` | 26 | yes | `UGameplayStatics::ApplyDamage(AActor*, float, AController*, AActor*, TSubclassOf<UDamageType>)` |
| `UHealthComponent::DecreaseHealth` | `0xaa503b0` | `0xac503b0` | 11 | yes | `UHealthComponent::DecreaseHealth(float, FString const&)` |
| `UHealthComponent::GetLocalHealth` | `0xaa50630` | `0xac50630` | 2 | yes | `UHealthComponent::GetLocalHealth() const` |
| `UHealthComponent::StaticClass` | `0x9fb5160` | `0xa1b5160` | 6 | yes | `UHealthComponent::StaticClass()` |
| `ADominionAICharacter::StaticClass` | `0x9f30ed0` | `0xa130ed0` | 1 | yes | `ADominionAICharacter::StaticClass()` |
