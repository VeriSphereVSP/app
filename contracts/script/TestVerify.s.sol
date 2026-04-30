// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Script.sol";
import "@openzeppelin/contracts/metatx/ERC2771Forwarder.sol";

contract TestVerify is Script {
    function run() external {
        uint256 pk = vm.envUint("BATCH_PRIVATE_KEY");
        address signer = vm.addr(pk);
        address fwd = vm.envAddress("FORWARDER_ADDRESS");
        address target = vm.envAddress("VSP_TOKEN");

        console.log("Signer:", signer);
        console.log("Forwarder (proxy):", fwd);
        console.log("Target:", target);

        // Read nonce
        uint256 nonce = ERC2771Forwarder(fwd).nonces(signer);
        console.log("Nonce:", nonce);

        // Build request
        uint48 deadline = uint48(block.timestamp + 300);
        bytes memory data = hex"00";

        // Sign using EIP-712
        bytes32 TYPEHASH = keccak256(
            "ForwardRequest(address from,address to,uint256 value,uint256 gas,uint256 nonce,uint48 deadline,bytes data)"
        );

        bytes32 structHash = keccak256(abi.encode(
            TYPEHASH,
            signer,
            target,
            uint256(0),
            uint256(1500000),
            nonce,
            uint256(deadline),
            keccak256(data)
        ));

        // Get domain separator from the proxy
        bytes32 domainSep = _getDomainSeparator(fwd);
        console.log("Domain separator:");
        console.logBytes32(domainSep);
        console.log("Struct hash:");
        console.logBytes32(structHash);

        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", domainSep, structHash));
        console.log("Digest:");
        console.logBytes32(digest);

        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, digest);
        bytes memory signature = abi.encodePacked(r, s, v);
        console.log("Signature length:", signature.length);

        // Build ForwardRequestData
        ERC2771Forwarder.ForwardRequestData memory req = ERC2771Forwarder.ForwardRequestData({
            from: signer,
            to: target,
            value: 0,
            gas: 1500000,
            deadline: deadline,
            data: data,
            signature: signature
        });

        // Call verify
        bool valid = ERC2771Forwarder(fwd).verify(req);
        console.log("verify():", valid);

        if (!valid) {
            console.log("FAILED - trying with manual domain separator computation");
            // Compute manually
            bytes32 TYPE_HASH = keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
            bytes32 manualDs = keccak256(abi.encode(
                TYPE_HASH,
                keccak256("VerisphereForwarder"),
                keccak256("1"),
                block.chainid,
                fwd
            ));
            console.log("Manual domain sep:");
            console.logBytes32(manualDs);
            console.log("Match:", manualDs == domainSep);
        }
    }

    function _getDomainSeparator(address fwd) internal view returns (bytes32) {
        // Call eip712Domain() and compute separator
        bytes32 TYPE_HASH = keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
        (,string memory name, string memory version, uint256 chainId, address verifyingContract,,) = 
            ERC2771Forwarder(fwd).eip712Domain();
        return keccak256(abi.encode(
            TYPE_HASH,
            keccak256(bytes(name)),
            keccak256(bytes(version)),
            chainId,
            verifyingContract
        ));
    }
}
