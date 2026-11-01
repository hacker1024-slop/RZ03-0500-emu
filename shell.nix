{ pkgs ? import <nixpkgs> {} }:
pkgs.mkShell {
  packages = [ pkgs.renode pkgs.python313 pkgs.python313Packages.pip
               pkgs.python313Packages.pytest pkgs.python313Packages.pyside6 pkgs.qt6.qtwayland
               pkgs.ruff pkgs.gcc-arm-embedded pkgs.cmake pkgs.ninja ];
  PYRENODE_PATH = "${pkgs.renode}/lib/renode";
  DOTNET_ROOT = "${pkgs.dotnetCorePackages.runtime_10_0}/share/dotnet";
}
