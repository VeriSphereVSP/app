// SPDX-License-Identifier: BUSL-1.1
pragma solidity ^0.8.20;

import "forge-std/Script.sol";
import "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import "../VerisphereForwarder.sol";

/// @notice Deploy or upgrade the VerisphereForwarder behind a UUPS proxy.
///
/// Modes:
///   Fresh:   Deploys impl + proxy. Writes proxy address to forwarder.json.
///   Upgrade: Reads existing proxy from forwarder.json, deploys new impl,
///            calls upgradeToAndCall. Proxy address unchanged.
///
/// Environment variables:
///   VSP_TOKEN_ADDRESS  — deployed VSP token proxy address (fresh only)
///   TREASURY_ADDRESS   — address to receive relay fees (fresh only)
///   FEE_BPS            — fee in basis points (default: 50, fresh only)
///   MIN_FEE_WEI        — minimum fee in VSP wei (default: 1e17, fresh only)
///   FORWARDER_MODE     — "fresh" or "upgrade" (default: auto-detect from forwarder.json)
contract DeployForwarder is Script {
    function run() external {
        uint256 pk = vm.envUint("PRIVATE_KEY");
        address deployer = vm.addr(pk);

        // Auto-detect mode: if forwarder.json exists with a valid address, upgrade
        bool isFresh = true;
        address existingProxy = address(0);
        try vm.readFile("deployments/forwarder.json") returns (string memory json) {
            try vm.parseJsonAddress(json, ".Forwarder") returns (address proxy) {
                if (proxy != address(0)) {
                    existingProxy = proxy;
                    isFresh = false;
                }
            } catch {}
        } catch {}

        // Allow override via env
        string memory modeOverride = vm.envOr("FORWARDER_MODE", string("auto"));
        if (keccak256(bytes(modeOverride)) == keccak256("fresh")) isFresh = true;
        if (keccak256(bytes(modeOverride)) == keccak256("upgrade")) isFresh = false;

        vm.startBroadcast(pk);

        if (isFresh) {
            // ── Fresh deploy: impl + proxy ──
            address vspToken = vm.envAddress("VSP_TOKEN_ADDRESS");
            address treasury = vm.envAddress("TREASURY_ADDRESS");
            uint256 feeBps = vm.envOr("FEE_BPS", uint256(50));
            uint256 minFeeWei = vm.envOr("MIN_FEE_WEI", uint256(1e17));  // 0.1 VSP

            VerisphereForwarder impl = new VerisphereForwarder();

            ERC1967Proxy proxy = new ERC1967Proxy(
                address(impl),
                abi.encodeCall(
                    VerisphereForwarder.initialize,
                    (vspToken, treasury, deployer, feeBps, minFeeWei)
                )
            );

            address proxyAddr = address(proxy);
            console.log("VerisphereForwarder (UUPS proxy) deployed at:", proxyAddr);
            console.log("  Implementation:", address(impl));
            console.log("  VSP Token:", vspToken);
            console.log("  Treasury:", treasury);
            console.log("  Fee:", feeBps, "bps");
            console.log("  Min fee:", minFeeWei, "wei");

            vm.stopBroadcast();

            string memory json = string.concat(
                '{"Forwarder":"', vm.toString(proxyAddr),
                '","ForwarderImpl":"', vm.toString(address(impl)),
                '"}'
            );
            vm.writeFile("deployments/forwarder.json", json);
            console.log("Wrote deployments/forwarder.json");

        } else {
            // ── Upgrade: deploy new impl, upgrade proxy ──
            console.log("Upgrading existing forwarder proxy:", existingProxy);

            VerisphereForwarder newImpl = new VerisphereForwarder();
            VerisphereForwarder(existingProxy).upgradeToAndCall(
                address(newImpl),
                bytes("")
            );

            console.log("  New implementation:", address(newImpl));
            console.log("  Proxy address unchanged:", existingProxy);

            vm.stopBroadcast();

            // Update impl address in forwarder.json (proxy stays same)
            string memory json = string.concat(
                '{"Forwarder":"', vm.toString(existingProxy),
                '","ForwarderImpl":"', vm.toString(address(newImpl)),
                '"}'
            );
            vm.writeFile("deployments/forwarder.json", json);
            console.log("Updated deployments/forwarder.json");
        }
    }
}
