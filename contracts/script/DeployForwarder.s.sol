// SPDX-License-Identifier: BUSL-1.1
pragma solidity ^0.8.20;

import "forge-std/Script.sol";
import "../VerisphereForwarder.sol";

/// @notice Deploy the VerisphereForwarder.
///         This is separate from the core protocol deployment.
///
/// Usage:
///   cd app
///   forge script contracts/script/DeployForwarder.s.sol \
///     --rpc-url $RPC --broadcast --private-key $PRIVATE_KEY
///
/// Environment variables:
///   VSP_TOKEN_ADDRESS  — deployed VSP token proxy address
///   TREASURY_ADDRESS   — address to receive relay fees (typically MM wallet)
///   FEE_BPS            — fee in basis points (default: 50 = 0.5%)
///   MIN_FEE_WEI        — minimum fee in VSP wei (default: 1e15 = 0.001 VSP)
contract DeployForwarder is Script {
    function run() external {
        address vspToken = vm.envAddress("VSP_TOKEN_ADDRESS");
        address treasury = vm.envAddress("TREASURY_ADDRESS");
        uint256 feeBps = vm.envOr("FEE_BPS", uint256(50));           // 0.5%
        uint256 minFeeWei = vm.envOr("MIN_FEE_WEI", uint256(1e17));  // 0.1 VSP

        uint256 pk = vm.envUint("PRIVATE_KEY");
        vm.startBroadcast(pk);

        VerisphereForwarder forwarder = new VerisphereForwarder(
            vspToken,
            treasury,
            feeBps,
            minFeeWei
        );

        console.log("VerisphereForwarder deployed at:", address(forwarder));
        console.log("  VSP Token:", vspToken);
        console.log("  Treasury:", treasury);
        console.log("  Fee:", feeBps, "bps");
        console.log("  Min fee:", minFeeWei, "wei");

        vm.stopBroadcast();

        // Write address to file for app config
        string memory json = string.concat(
            '{"Forwarder":"', vm.toString(address(forwarder)), '"}'
        );
        vm.writeFile("deployments/forwarder.json", json);
        console.log("Wrote deployments/forwarder.json");
    }
}
