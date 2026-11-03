{ pkgs ? import <nixpkgs> {} }:
let
  python = pkgs.python313.withPackages (ps:
    let
      # Use the upstream wheels to avoid building the CLR bridge and its
      # .NET test fixtures against the shell's configured nixpkgs.
      clr-loader = ps.buildPythonPackage {
        pname = "clr-loader";
        version = "0.3.1";
        format = "wheel";
        src = pkgs.fetchurl {
          url = "https://files.pythonhosted.org/packages/5e/da/ec1a6e36624000b6df0dd61183c42342ee5814c073315e802cadaad04d2f/clr_loader-0.3.1-py3-none-any.whl";
          sha256 = "cbad189de20d202a7d621956b0fc38049e13c9bf7ca2923441eff725cd121aa1";
        };
        dependencies = [ ps.cffi ];
      };
      pythonnet = ps.buildPythonPackage {
        pname = "pythonnet";
        version = "3.2.1";
        format = "wheel";
        src = pkgs.fetchurl {
          url = "https://files.pythonhosted.org/packages/d8/dc/c96a542079a69d3d091e91808fc8aaf9a0d5e2f8e73a63560e4379134bb6/pythonnet-3.2.1-cp311.cp312.cp313.cp314.cp315-none-any.whl";
          sha256 = "dd5668b74b0c903d9a24e1ba86f728fc490f8e919d56b3c24bf06739b9ee2b7f";
        };
        dependencies = [ clr-loader ];
      };
      pyrenode3 = ps.buildPythonPackage {
        pname = "pyrenode3";
        version = "0.0.1";
        pyproject = true;
        # Keep this revision aligned with pyproject.toml.
        src = pkgs.fetchFromGitHub {
          owner = "antmicro";
          repo = "pyrenode3";
          rev = "27362def38ce409b95c972ce494cd8517dac17a4";
          hash = "sha256-XyqF0PW0XWRV/f2L2sb967AGUEHddn1s7h3r6LQ0fCc=";
        };
        build-system = [ ps.hatchling ];
        dependencies = [ pythonnet ];
      };
    in [ pyrenode3 ps.pip ps.pytest ps.pyside6 ]);
in
pkgs.mkShell {
  packages = [ pkgs.renode python pkgs.qt6.qtwayland pkgs.ruff pkgs.gcc-arm-embedded ];
  PYRENODE_PATH = "${pkgs.renode}/lib/renode";
  DOTNET_ROOT = "${pkgs.dotnetCorePackages.runtime_10_0}/share/dotnet";
}
